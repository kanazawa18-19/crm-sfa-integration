import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "営業管理ダッシュボード",
  description: "CRM/SFA 社内管理画面",
  robots: { index: false, follow: false, nocache: true },
};

// web-engagement-tool(MA)のapp/layout.tsxと同じ仕組み(2026-08-15移植)。Resolves the
// stored (or OS) theme preference to a concrete data-theme="light"|"dark" before
// first paint, so there's no flash of the wrong theme and so Tailwind's dark:
// variant (keyed off this attribute — see globals.css) is correct from the very
// first frame.
const THEME_INIT_SCRIPT = `(function(){try{var s=localStorage.getItem("dashboard-theme");var t=(s==="light"||s==="dark")?s:(window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.setAttribute("data-theme",t);}catch(e){}})();`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="ja"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-full flex flex-col bg-(--color-background)">{children}</body>
    </html>
  );
}
