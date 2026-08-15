// web-engagement-tool(MA)のBrandLogo.tsxと同じ実装(2026-08-15移植、ロゴ画像ファイルも
// 同一のものをそのままコピーして使用)。Fixed-size box + object-contain so the CNCTOR
// wordmark never gets stretched/squished by a flex parent's default
// align-items:stretch.
export default function BrandLogo({
  heightClass,
  widthClass,
  className,
}: {
  heightClass: string;
  widthClass: string;
  className?: string;
}) {
  return (
    <span className={`relative block shrink-0 ${widthClass} ${heightClass} ${className ?? ""}`}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/brand/cnctor-logo-light.png"
        alt="CNCTOR"
        className="absolute inset-0 block h-full w-full object-contain object-left dark:hidden"
      />
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/brand/cnctor-logo-dark.png"
        alt="CNCTOR"
        className="absolute inset-0 hidden h-full w-full object-contain object-left dark:block"
      />
    </span>
  );
}
