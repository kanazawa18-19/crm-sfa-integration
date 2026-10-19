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
