---
id: plugin-ast-check-bypass-via-library-attributes
title: plugin がライブラリの属性経由で os に届き、コード検査を回避できる
status: 設計待ち
priority: 中
opened: 2026-10-02
closed: null
related: []
---
# [plugin-ast-check-bypass-via-library-attributes] plugin がライブラリの属性経由で os に届き、コード検査を回避できる

**状態**: 設計待ち / **優先**: 中

## 現象

plugin のコード検査 (import の許可リストと AST 検査) は、許可されたライブラリが内部に持つモジュール参照を辿る書き方を止められない。例: pandas が読み込み済みの os を属性で辿って os._exit を呼べる (2026-10-02、受入テストの異常終了ケースで実際に通過)。

## 原因

AST 検査は import 文と名前の直接参照を見るが、許可済みオブジェクトの属性チェーンは追っていない。レビューでは既知の制限と判定。

## 処置案・裁定

実際の防御は worker の Landlock・リソース上限・ネットワーク遮断が担う。検査を強化するか (属性チェーンの拒否、危険な属性名の遮断)、検査は補助と位置づけて防御側の保証を文書化するかを決める。Landlock 下で os 経由に何ができて何ができないかを実測してから判断する。

## 修正内容

## 経緯

- 2026-10-02: 起票。
