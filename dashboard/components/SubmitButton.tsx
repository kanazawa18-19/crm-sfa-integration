"use client";

import { useFormStatus } from "react-dom";

// web-engagement-tool(MA)にも同名・同用途のコンポーネントがあるが、こちらは独立実装。
// 複数送信ボタン対応(name/value)が必要になったらMA側の実装
// (src/components/SubmitButton.tsx)を参照。
export default function SubmitButton({
  children,
  pendingLabel,
  className,
}: {
  children: React.ReactNode;
  pendingLabel: string;
  className?: string;
}) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className={className}>
      {pending ? pendingLabel : children}
    </button>
  );
}
