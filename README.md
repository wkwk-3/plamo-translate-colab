# plamo-translate-colab

PLaMo 3 Translate（`plamo-3-translate-2b-Q4_K_M`）を Google Colab の GPU で動かし、Minecraft 翻訳向けの OpenAI 互換 API（`/v1/chat/completions`）として公開するノートブックです。

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/wkwk-3/plamo-translate-colab/blob/main/colab_openai_api.ipynb)

## ファイル

| ファイル | 内容 |
| --- | --- |
| `colab_openai_api.ipynb` | Colab 用ノートブック。llama-server・翻訳プロキシ・Cloudflare Tunnel を起動する。要求と応答の形式はノートブック冒頭に記載 |
| `plamo_proxy_local.py` | 自分の PC の LM Studio に接続するローカル版プロキシ |

## Colab 版の使い方

1. 上のボタンからノートブックを開き、ランタイムのタイプで GPU（T4 など）を選ぶ
2. セルを上から順に実行する
3. ③で表示される接続先 URL・API キー・モデル名を、クライアントの OpenAI 互換 API 設定に入力する
4. 長時間使う場合は⑥を実行したままにする

③の出力には API キーが表示されます。ノートブックを保存・共有する前に出力を消去してください。

## ローカル版について

`plamo_proxy_local.py` は Colab 版とは別の実装です。LM Studio（`http://127.0.0.1:1234`）で plamo-3-translate を読み込んだうえで、次のように起動します。

```bash
python plamo_proxy_local.py -h
```

Colab 版のプロキシにある次の機能は、ローカル版にはありません。

- 書式コード・placeholder の復元後の照合と、別方式での再試行
- Patchouli・FTB Quests 用の保護パターン（`entry_kind`）
- 1 要求あたりの処理時間の上限（`BATCH_DEADLINE`）
