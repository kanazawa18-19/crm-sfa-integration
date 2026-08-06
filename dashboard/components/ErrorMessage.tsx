export default function ErrorMessage({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-red-700">
      <p className="font-medium">データの取得に失敗しました</p>
      <p className="mt-1 text-sm">{message}</p>
    </div>
  );
}
