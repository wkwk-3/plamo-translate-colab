"""翻訳ツール ⇔ LM Studio(PLaMo 3 Translate) の間に入るプロキシ（ローカル版）。

  翻訳ツール ──> http://127.0.0.1:8100/v1 (このプロキシ) ──> http://127.0.0.1:1234/v1 (LM Studio)

- OpenAI 互換のバッチ翻訳リクエスト(JSON の entries)を 1 件ずつ PLaMo に投げ、まとめて返す
- LM Studio は chat_template_kwargs を渡せないので、PLaMo 公式の
  "[plamo-translate]" system メッセージ形式で target_lang / glossary / context を渡す
- %s / §c / {0} / 改行 をマスクして壊れないように保護
- 用語集はその行の原文に出てくる語だけを渡す（コンテキスト節約・精度向上）
- /v1/models 対応、任意の API キー認証、requests.jsonl に記録

使い方:
  1. LM Studio で plamo-3-translate を読み込み、サーバーを起動 (:1234)
  2. python plamo_proxy_local.py [--parallel 4] [--form plain] ...   (-h で一覧)
  3. 翻訳ツールの接続先を http://127.0.0.1:8100/v1 にする（モデル名は何でも可）

標準ライブラリのみ。
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ===== 設定（起動引数で上書き。python plamo_proxy_local.py -h で一覧）=====
UPSTREAM = "http://127.0.0.1:1234"  # LM Studio
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8100
PLAMO_MODEL = ""  # 空なら LM Studio から plamo を自動検出
PARALLEL = 4  # LM Studio の Max Concurrent と合わせる
API_KEY = ""  # 空なら認証なし
MAX_TOKENS = 512
TEMPERATURE = 0.0
TIMEOUT = 120
FORM = ""  # "", "plain", "polite"（日本語訳のときのみ）
GLOSSARY_FILTER = True  # 原文に出てくる用語だけ渡す
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requests.jsonl")
# ================

LANG = {"zh_cn": "zh-hans", "zh_tw": "zh-hant", "zh_hk": "zh-hant"}

# printf系 / Minecraft書式コード / {0} / 改行
PH = re.compile(
    r"%(?:\d+\$)?[-#+ 0,(]*\d*(?:\.\d+)?[sdfxXcbeEgGoh%]"
    r"|§[0-9a-fk-orA-FK-OR]"
    r"|\{\d+\}"
    r"|\\n|\n"
)
FULLWIDTH = str.maketrans({"｛": "{", "｝": "}", "０": "0", "１": "1", "２": "2", "３": "3",
                           "４": "4", "５": "5", "６": "6", "７": "7", "８": "8", "９": "9"})


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def record(req, res, code):
    entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "status": code,
             "request": req, "response": res}
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def number_s(src: str) -> str:
    """番号なし %s/%d を %1$s, %2$d ... に（語順入替で意味が逆になるのを防ぐ）。%% は除外。"""
    if re.search(r"%\d+\$", src):
        return src
    n = iter(range(1, 100))
    return re.sub(r"(?<!%)%([sd])", lambda m: f"%{next(n)}${m.group(1)}", src)


def mask(src: str):
    phs = []

    def rep(m):
        phs.append(m.group(0))
        return f"{{{len(phs) - 1}}}"

    return PH.sub(rep, src), phs


def unmask(t: str, phs):
    t = t.translate(FULLWIDTH)
    t = re.sub(r"\{\s*(\d+)\s*\}", r"{\1}", t)
    if sorted(map(int, re.findall(r"\{(\d+)\}", t))) != list(range(len(phs))):
        return None
    for i, p in enumerate(phs):
        t = t.replace(f"{{{i}}}", p)
    return t


def to_lang(locale: str) -> str:
    loc = locale.lower().replace("-", "_")
    return LANG.get(loc, loc.split("_")[0])


def to_glossary(g):
    """配列 [{source,target}] / [{term,translation}] / 辞書 {src: dst} のどれでも受け付ける"""
    out = []
    if isinstance(g, dict):
        g = [{"source": k, "target": v} for k, v in g.items()]
    for item in g or []:
        if isinstance(item, dict):
            s = item.get("source") or item.get("term") or item.get("src")
            t = item.get("target") or item.get("translation") or item.get("dst")
            if s and t:
                out.append({"source": str(s), "target": str(t)})
    return out


def pick_glossary(src: str, glossary):
    if not GLOSSARY_FILTER:
        return glossary
    low = src.lower()
    return [g for g in glossary if g["source"].lower() in low]


# ---- LM Studio 呼び出し ----

def http_json(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def detect_model():
    global PLAMO_MODEL
    if PLAMO_MODEL:
        return PLAMO_MODEL
    ids = [m["id"] for m in http_json(UPSTREAM + "/v1/models").get("data", [])]
    cand = [i for i in ids if "plamo" in i.lower() and "translate" in i.lower()] or \
           [i for i in ids if "plamo" in i.lower()]
    if not cand:
        raise RuntimeError(f"LM Studio に plamo のモデルが見つかりません: {ids}")
    PLAMO_MODEL = cand[0]
    log(f"model: {PLAMO_MODEL}")
    return PLAMO_MODEL


def build_system(target_lang, context, glossary):
    lines = ["[plamo-translate]", f"target_lang={target_lang}"]
    if FORM and target_lang == "ja":
        lines.append(f"form={FORM}")
    for g in glossary:
        lines.append(f"glossary={g['source']} => {g['target']}")
    if context:
        lines += ["---", context]
    return "\n".join(lines)


def call_plamo(text, target_lang, context, glossary):
    body = {
        "model": detect_model(),
        "messages": [
            {"role": "system", "content": build_system(target_lang, context, glossary)},
            {"role": "user", "content": text},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stop": ["<|plamo:tag|>", "<|plamo:bos|>"],
        "stream": False,
    }
    return http_json(UPSTREAM + "/v1/chat/completions", body)["choices"][0]["message"]["content"].strip()


# ---- バッチ処理 ----

def translate_entry(e, batch, glossary):
    src = e["source"]
    numbered = number_s(src)
    masked, phs = mask(numbered)
    if not masked.strip("{}0123456789 "):
        return {"id": e["id"], "translation": numbered}
    ctx = f"Minecraft mod '{batch.get('mod_name', '')}' UI string. key: {e.get('key', '')}"
    try:
        out = call_plamo(masked, to_lang(e.get("target_locale", "ja_jp")), ctx, pick_glossary(src, glossary))
        t = unmask(out, phs)
    except Exception as ex:  # noqa: BLE001
        log(f"[ERROR] id={e['id']} {ex}")
        t = None
    if t is None:
        log(f"[FALLBACK] id={e['id']} key={e.get('key')} src={src!r}")
        t = numbered
    elif "§" in src:
        log(f"[CHECK §] id={e['id']} {src!r} -> {t!r}")
    return {"id": e["id"], "translation": t}


def wrap(req, content):
    return {
        "id": f"chatcmpl-plamo-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.get("model", PLAMO_MODEL),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def handle(req: dict) -> dict:
    last = req["messages"][-1]["content"]
    try:
        batch = json.loads(last)
        entries = batch["entries"]
    except Exception:  # バッチ形式でなければ日本語へ単発翻訳（接続テスト用）
        log("[PLAIN] non-batch request")
        return wrap(req, call_plamo(str(last), "ja", "", []))
    glossary = to_glossary(batch.get("glossary"))
    t0 = time.time()
    with ThreadPoolExecutor(PARALLEL) as ex:
        out = list(ex.map(lambda e: translate_entry(e, batch, glossary), entries))
    log(f"[BATCH] {len(entries)} entries, glossary {len(glossary)}, {time.time() - t0:.1f}s")
    return wrap(req, json.dumps({"translations": out}, ensure_ascii=False))


class H(BaseHTTPRequestHandler):
    def send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        if API_KEY and self.headers.get("Authorization", "") != f"Bearer {API_KEY}":
            self.send_json(401, {"error": {"message": "Invalid API key", "type": "invalid_request_error"}})
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        if not self.authed():
            return
        if self.path.rstrip("/").endswith("/models"):
            try:
                mid = detect_model()
            except Exception as ex:  # noqa: BLE001
                return self.send_json(502, {"error": {"message": str(ex)}})
            self.send_json(200, {"object": "list", "data": [
                {"id": mid, "object": "model", "created": 0, "owned_by": "pfnet"}]})
        else:
            self.send_json(200, {"status": "ok"})

    def do_POST(self):
        if not self.authed():
            return
        req = None
        try:
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if req.get("stream"):
                raise ValueError("stream is not supported")
            res, code = handle(req), 200
        except Exception as ex:  # noqa: BLE001
            log(f"[BAD REQUEST] {ex}")
            res, code = {"error": {"message": str(ex)}}, 400
        try:
            record(req, res, code)
        except Exception as ex:  # noqa: BLE001
            log(f"[LOG ERROR] {ex}")
        self.send_json(code, res)

    def log_message(self, *a):
        pass


def parse_args():
    import argparse
    p = argparse.ArgumentParser(description="翻訳ツール ⇔ LM Studio(PLaMo 3 Translate) プロキシ")
    p.add_argument("--upstream", default=UPSTREAM, help=f"LM Studio のアドレス (既定: {UPSTREAM})")
    p.add_argument("--host", default=LISTEN_HOST, help=f"待ち受けアドレス (既定: {LISTEN_HOST})")
    p.add_argument("--port", type=int, default=LISTEN_PORT, help=f"待ち受けポート (既定: {LISTEN_PORT})")
    p.add_argument("--model", default=PLAMO_MODEL, help="LM Studio 上のモデル名 (既定: 自動検出)")
    p.add_argument("--parallel", type=int, default=PARALLEL, help=f"同時翻訳数。LM Studio の Max Concurrent と合わせる (既定: {PARALLEL})")
    p.add_argument("--api-key", default=API_KEY, help="設定すると Bearer 認証を要求 (既定: なし)")
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS, help=f"1件あたりの最大出力トークン (既定: {MAX_TOKENS})")
    p.add_argument("--temperature", type=float, default=TEMPERATURE, help=f"(既定: {TEMPERATURE})")
    p.add_argument("--timeout", type=int, default=TIMEOUT, help=f"1件あたりのタイムアウト秒 (既定: {TIMEOUT})")
    p.add_argument("--form", choices=["", "plain", "polite"], default=FORM, help="日本語の文体 plain=だ・である / polite=です・ます (既定: モデル任せ)")
    p.add_argument("--no-glossary-filter", action="store_true", help="用語集を絞り込まず毎回全部渡す")
    p.add_argument("--log-file", default=LOG_FILE, help="記録ファイル (既定: スクリプトと同じフォルダの requests.jsonl)")
    a = p.parse_args()
    g = globals()
    g.update(UPSTREAM=a.upstream.rstrip("/").removesuffix("/v1"), LISTEN_HOST=a.host, LISTEN_PORT=a.port,
             PLAMO_MODEL=a.model, PARALLEL=max(1, a.parallel), API_KEY=a.api_key, MAX_TOKENS=a.max_tokens,
             TEMPERATURE=a.temperature, TIMEOUT=a.timeout, FORM=a.form,
             GLOSSARY_FILTER=not a.no_glossary_filter, LOG_FILE=a.log_file)


if __name__ == "__main__":
    parse_args()
    try:
        detect_model()
    except Exception as ex:  # noqa: BLE001
        log(f"[WARN] LM Studio に接続できません（起動後に自動で再試行します）: {ex}")
    log(f"listening http://{LISTEN_HOST}:{LISTEN_PORT}/v1 -> {UPSTREAM}  (log: {LOG_FILE})")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), H).serve_forever()
