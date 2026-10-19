import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Card } from "../review-list";

const row = { id: "r", revision: 0, subject: "合成", reason: "確認中", state: "held",
  delivery: "not_started", direction: "outbound", owner: "rep@example.test", gmail_id: "m",
  participants: [], contacts: [], candidates: [], evidence: [] };

describe("送信証拠と操作の表示", () => {
  it.each([undefined, false])("証拠 %s のとき不要な操作を隠し再取得案内を表示する", sent_evidence => {
    const html = renderToStaticMarkup(<Card row={{...row, sent_evidence}} reload={() => {}} />);
    expect(html).toContain("再取得を依頼");
    expect(html).toContain("送信状態未確認");
    expect(html).not.toContain("<fieldset");
    expect(html).not.toContain("関連先を確定</button>");
  });
  it("送信済みなら既存の関連先確定を使える", () => {
    const html = renderToStaticMarkup(<Card row={{...row, sent_evidence: true}} reload={() => {}} />);
    expect(html).toContain("関連先を確定</button>");
    expect(html).not.toContain("再取得を依頼");
  });
});

describe("固定訂正の予約表示", () => {
  it.each(["held", "unknown"])("%s の予約中は送信証拠があっても操作を表示しない", state => {
    const html = renderToStaticMarkup(<Card row={{...row, state, sent_evidence: true, correction_active: true}} reload={() => {}} />);
    expect(html).toContain("訂正処理中");
    expect(html).toContain("専用手順での再開");
    expect(html).not.toContain("<fieldset");
    expect(html).not.toContain("登録済みの記録を探す");
    expect(html).not.toContain("再取得を依頼");
  });
  it("取消済み下書きは理由を残し処理中や再開案内を表示しない", () => {
    const html = renderToStaticMarkup(<Card row={{...row, state: "excluded", sent_evidence: false,
      correction_active: true, reason: "下書き誤登録を取消しました。配送状態は取消前の履歴です"}} reload={() => {}} />);
    expect(html).toContain("取消しました");
    expect(html).toContain("取消前の履歴");
    expect(html).not.toContain("訂正処理中");
    expect(html).not.toContain("再開を依頼");
    expect(html).not.toContain("再取得を依頼");
    expect(html).not.toContain("<fieldset");
  });
  it("解除後の送信正本は通常操作へ戻る", () => {
    const html = renderToStaticMarkup(<Card row={{...row, sent_evidence: true, correction_active: false}} reload={() => {}} />);
    expect(html).toContain("関連先を確定</button>");
    expect(html).not.toContain("訂正処理中");
  });
});
