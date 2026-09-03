"""一斉配信のプレビュー組み立て（純粋関数、2026-09-03）。

**この関数はDBもNotionもGmailも触らない。** 必要なものは全部引数で受け取る。
一斉配信で一番壊してはいけない判断（誰に送るか・法定表示が揃っているか）を、
外部サービスを起動せずにテストできる状態に保つため。

```
   入力  件名 / 本文テンプレート / 連絡先の一覧 / 配信停止の一覧 / 送信者情報 / 署名鍵
                                  │
                                  ▼
   出力  messages   宛先ごとの差し込み済みの件名・本文（そのまま送れる形）
         skipped    外した宛先と理由
         blockers   これが1つでもある間は送ってはいけない
         warnings   送れるが知っておくべきこと
```

`blockers`があっても`messages`は組み立てて返す。**画面で本文を確認する用途は
潰さない**（送信者情報が空でも、文面のレビューはできた方がよい）。
送信側は`blockers`が空であることを必ず自分で確かめること。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from src.bulk_email import compliance, template, unsubscribe
from src.bulk_email.audience import (
    SKIP_MISSING_MERGE_VALUE,
    Contact,
    SkippedContact,
    normalize_email,
    select_recipients,
)


@dataclass(frozen=True)
class RenderedMessage:
    """1宛先ぶんの、そのまま送れる形。"""

    contact_page_id: str
    contact_name: str
    client_name: str
    to_email: str
    subject: str
    body: str


@dataclass(frozen=True)
class PreviewResult:
    messages: tuple[RenderedMessage, ...] = ()
    skipped: tuple[SkippedContact, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # 本文で実際に使われている差し込み名（画面のヘルプ表示用）。
    placeholders_used: tuple[str, ...] = ()

    @property
    def sendable(self) -> bool:
        return not self.blockers and bool(self.messages)


@dataclass
class _Collector:
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _check_inputs(subject: str, body: str, collector: _Collector) -> None:
    if not (subject or "").strip():
        collector.blockers.append("件名が空です。")
    if not (body or "").strip():
        collector.blockers.append("本文が空です。")

    unknown = template.unknown_placeholders(subject) + template.unknown_placeholders(body)
    if unknown:
        names = "・".join(dict.fromkeys(unknown))
        usable = "・".join(template.PLACEHOLDERS)
        collector.blockers.append(
            f"知らない差し込み名があります: {{{{{names}}}}}。使えるのは {usable} です。"
        )

    if "\n" in (subject or "") or "\r" in (subject or ""):
        # メールの件名はヘッダー1行なので、そもそも改行を含められない。
        # ②で実送信を足したとき、改行入りの件名をそのままヘッダーへ渡すと
        # 任意のヘッダー（Bcc等）を差し込まれる余地になる。送信コードが無いうちから
        # 入口で止めておく（shirokuma-secレビューINFO、2026-09-03）。
        collector.blockers.append("件名に改行は入れられません。1行にしてください。")

    if compliance.contains_footer(body):
        collector.blockers.append(
            "本文に法定表示（送信者・配信停止）が既に含まれています。"
            "過去のメールを貼り付けると、そこに残っている配信停止リンクは別の宛先のものです。"
            "テンプレートからは外してください（送信時に自動で付きます）。"
        )


def build_preview(
    *,
    subject: str,
    body: str,
    contacts: Sequence[Contact],
    sender_name: str = "",
    identity: compliance.SenderIdentity,
    unsubscribe_secret: str = "",
    unsubscribe_base_url: str = "",
    opted_out_page_ids: Iterable[str] = (),
    opted_out_emails: Iterable[str] = (),
    truncated_client_names: Sequence[str] = (),
) -> PreviewResult:
    """プレビューを組み立てる。"""
    collector = _Collector()
    _check_inputs(subject, body, collector)

    missing_sender = compliance.missing_sender_fields(identity)
    if missing_sender:
        collector.blockers.append(
            "送信者情報が未設定です（"
            + "・".join(missing_sender)
            + "）。特定電子メール法で本文への表示が義務づけられているため、"
            "config/bulk_email_sender.json を埋めるまで送れません。"
        )

    if not unsubscribe_secret:
        collector.blockers.append(
            "配信停止リンクの署名鍵（BULK_EMAIL_UNSUBSCRIBE_SECRET）が未設定です。"
            "停止できないメールは送れません。"
        )
    if not unsubscribe_base_url:
        collector.blockers.append(
            "配信停止ページのURL（DASHBOARD_BASE_URL）が未設定です。"
            "停止できないメールは送れません。"
        )

    if truncated_client_names:
        collector.warnings.append(
            "連絡先が多く、次の取引先は先頭までしか読み込めていません: "
            + "・".join(truncated_client_names)
            + "。全員に送るには絞り込むか、宛先の作り方を見直してください。"
        )

    recipients, skipped = select_recipients(
        contacts,
        opted_out_page_ids=opted_out_page_ids,
        opted_out_emails=opted_out_emails,
    )

    messages: list[RenderedMessage] = []
    for contact in recipients:
        values = {
            "会社名": contact.client_name,
            "氏名": contact.name,
            "部署": contact.department,
            "役職": contact.title,
            "担当者名": sender_name,
        }
        rendered_subject = template.render(subject, values)
        rendered_body = template.render(body, values)
        missing = tuple(dict.fromkeys(rendered_subject.missing + rendered_body.missing))
        if missing:
            # 「〇〇様」のつもりが「　様」になったメールを送るくらいなら送らない。
            skipped.append(
                SkippedContact(contact, SKIP_MISSING_MERGE_VALUE, "・".join(missing))
            )
            continue

        url = ""
        if unsubscribe_secret and unsubscribe_base_url:
            url = unsubscribe.build_unsubscribe_url(
                unsubscribe_base_url,
                contact.page_id,
                unsubscribe.build_token(unsubscribe_secret, contact.page_id),
            )
        footer = compliance.build_footer(identity, url)

        messages.append(
            RenderedMessage(
                contact_page_id=contact.page_id,
                contact_name=contact.name,
                client_name=contact.client_name,
                to_email=normalize_email(contact.email),
                subject=rendered_subject.text,
                body=compliance.append_footer(rendered_body.text, footer),
            )
        )

    if not messages:
        collector.blockers.append("送れる宛先が1件もありません。")

    placeholders_used = tuple(
        dict.fromkeys(template.find_placeholders(subject) + template.find_placeholders(body))
    )

    return PreviewResult(
        messages=tuple(messages),
        skipped=tuple(skipped),
        blockers=tuple(collector.blockers),
        warnings=tuple(collector.warnings),
        placeholders_used=placeholders_used,
    )
