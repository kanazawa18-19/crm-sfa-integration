"use client";

import { useFormStatus } from "react-dom";

/**
 * iOS風のピル型トグルスイッチとして見せるsubmitボタン。
 * SubmitButton(components/SubmitButton.tsx)と同じ思想(useFormStatusのpendingで
 * 処理中フィードバックを出す)を踏襲しつつ、テキストリンクではなく
 * role="switch"のON/OFFスイッチとしての見た目・意味論が必要な用途向けに
 * 専用実装した(users/page.tsx「インシデント通知先」列、2026-08-26)。
 *
 * <form action={serverAction}>の中に置く1つのbutton要素で、クリック=フォーム送信
 * =ON/OFF切り替えというアーキテクチャ(サーバーで状態を書き換えて再レンダリング)は
 * 変えていない。楽観的更新やAPIルート化はしていない。
 *
 * disabled=trueの場合(例: 最後の1人はOFFにできないガード)はformで囲わずに
 * 単体でも使える。useFormStatusは親に<form>が無い場合pending: falseを返すだけで
 * エラーにはならない(React 19の仕様)。
 *
 * pending中は「クリック後の状態」を楽観的に先取り表示する(トラック色・つまみ位置を
 * checkedの反転先へ動かし、さらにパルスさせる)。サーバーの実際の再レンダリング完了
 * より前に見た目だけ動かす楽観的UIだが、この用途はON/OFFの単純反転かつ失敗時は
 * pending解除後に元のcheckedへ自動的に戻るため、実データとの矛盾は生じない
 * (obasan-qualityレビュー指摘、2026-08-26 — pending中もつまみ位置が変化前のままで
 * 「反応していない」ように見える問題への対処)。
 */
export default function SwitchButton({
  checked,
  label,
  pendingLabel,
  disabled,
  describedById,
}: {
  checked: boolean;
  label: string;
  pendingLabel: string;
  disabled?: boolean;
  describedById?: string;
}) {
  const { pending } = useFormStatus();
  const isDisabled = disabled || pending;
  const stateText = pending ? pendingLabel : checked ? "ON" : "OFF";
  // pending中は反転後の見た目を先取りして「押した」フィードバックを出す
  const visualChecked = pending ? !checked : checked;

  return (
    <span className="inline-flex items-center gap-2">
      <button
        type="submit"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        aria-describedby={describedById}
        disabled={isDisabled}
        className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-(--brand-blue-light) focus-visible:ring-offset-2 focus-visible:ring-offset-(--color-surface) disabled:cursor-not-allowed disabled:opacity-50 ${
          pending ? "animate-pulse" : ""
        } ${
          visualChecked
            ? "border-(--brand-blue) bg-(--brand-blue)"
            : "border-(--border-subtle) bg-(--color-surface-muted)"
        }`}
      >
        <span
          aria-hidden="true"
          className={`inline-block h-4 w-4 transform rounded-full bg-white shadow-sm transition-transform duration-150 ${
            visualChecked ? "translate-x-6" : "translate-x-1"
          }`}
        />
      </button>
      <span
        className={`text-xs font-semibold tabular-nums ${
          pending
            ? "text-(--color-foreground)/50"
            : checked
              ? "text-(--brand-blue-dark)"
              : "text-(--text-grey)"
        }`}
        aria-hidden="true"
      >
        {stateText}
      </span>
    </span>
  );
}
