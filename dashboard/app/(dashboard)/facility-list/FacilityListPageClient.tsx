"use client";

import { useState } from "react";
import ErrorMessage from "@/components/ErrorMessage";
import type { FacilityListExport, FacilityListPreview } from "@/lib/backend";
import { isSessionExpiredResponse, SESSION_EXPIRED_MESSAGE } from "@/lib/sessionCheck";

const PREFECTURES = [
  "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
  "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
  "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
  "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
  "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
  "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
  "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
];

const CATEGORIES = [
  { value: "hotel", label: "ホテル" },
  { value: "ryokan", label: "旅館・温泉宿" },
  { value: "pension", label: "ペンション・民宿" },
  { value: "villa", label: "貸別荘・コテージ" },
  { value: "unknown", label: "不明" },
];

// 「提案済みなら外す」で選べる商材。Zohoの取引サービス(config/zoho_picklists.json)と
// 提案資料の商材名に合わせてある。
const PRODUCTS = [
  "ホテルラボ", "ホテルラボRM", "ホテルラボ レビュー", "レセリア", "リピッテホテル",
  "メイリー", "フルスコ", "Growth Cube", "ILCA", "LevGo", "クリエイティブラボ",
  "WEB制作（楽天CP）", "パーソネル",
];

type Criteria = {
  prefectures: string[];
  room_count_min: number;
  room_count_max: number | null;
  review_min: number | null;
  review_max: number | null;
  include_unrated: boolean;
  categories: string[];
  custom_page: string | null;
  check_in_machine: string | null;
  has_onsen: boolean | null;
  min_review_count: number | null;
  max_photo_count: number | null;
  crm_filter: string;
  exclude_proposed_services: string[];
  exclude_chains: boolean;
  limit: number | null;
};

const INITIAL: Criteria = {
  prefectures: [],
  room_count_min: 1,
  room_count_max: null,
  review_min: null,
  review_max: null,
  include_unrated: false,
  categories: [],
  custom_page: null,
  check_in_machine: null,
  has_onsen: null,
  min_review_count: null,
  max_photo_count: null,
  crm_filter: "any",
  exclude_proposed_services: [],
  exclude_chains: false,
  limit: null,
};

function toNumberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function toggle(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

export default function FacilityListPageClient() {
  const [criteria, setCriteria] = useState<Criteria>(INITIAL);
  const [preview, setPreview] = useState<FacilityListPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exportNote, setExportNote] = useState<string | null>(null);

  const update = <K extends keyof Criteria>(key: K, value: Criteria[K]) => {
    setCriteria((current) => ({ ...current, [key]: value }));
    // 条件を変えたら前回の件数は無効。出したままにすると古い件数で判断させてしまう。
    setPreview(null);
    setExportNote(null);
  };

  const usesCrmCondition =
    criteria.crm_filter !== "any" || criteria.exclude_proposed_services.length > 0;

  async function call<T>(path: string): Promise<T | null> {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(criteria),
      redirect: "manual",
    });
    if (isSessionExpiredResponse(response)) {
      setError(SESSION_EXPIRED_MESSAGE);
      return null;
    }
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      setError(payload?.detail ?? "処理に失敗しました");
      return null;
    }
    return payload as T;
  }

  async function handlePreview() {
    setError(null);
    setPreviewing(true);
    try {
      const result = await call<FacilityListPreview>("/api/facility-list/preview");
      if (result) setPreview(result);
    } finally {
      setPreviewing(false);
    }
  }

  async function handleExport() {
    setError(null);
    setExportNote(null);
    setExporting(true);
    try {
      const result = await call<FacilityListExport>("/api/facility-list/export");
      if (!result) return;
      // ブラウザにそのまま保存させる。CSVはBOM付きUTF-8でバックエンドが作っている。
      const blob = new Blob([result.csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      const stamp = new Date().toISOString().slice(0, 10);
      anchor.href = url;
      anchor.download = `営業リスト_${stamp}.csv`;
      anchor.click();
      URL.revokeObjectURL(url);

      // 何件がどういう状態だったかを必ず伝える。特に「突合を打ち切った件数」を
      // 黙っていると、その行が新規リストに入っていないことに気づけない。
      const notes = [`${result.total}件を書き出しました`];
      notes.push(`既存取引先 ${result.matched_count}件`);
      notes.push(`未取引の可能性 ${result.new_count}件`);
      if (result.ambiguous_count > 0) {
        notes.push(`要確認（候補が複数） ${result.ambiguous_count}件`);
      }
      if (result.unchecked_count > 0) {
        notes.push(
          `時間切れでCRM突合できなかった ${result.unchecked_count}件（この行は新規リストに入っていません）`
        );
      }
      if (!result.contacts_available) {
        notes.push("CRMに接続できなかったため、担当者の連絡先は空欄です");
      }
      setExportNote(notes.join(" / "));
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="page-title">リスト作成</h1>
        <p className="mt-1 text-sm text-(--color-foreground)/60">
          楽天トラベルに掲載されている宿泊施設から、条件に合う営業リストを作ります。
          施設カテゴリーは施設名と設備からの<strong>推定</strong>で、楽天側に項目として
          あるものではありません。
        </p>
      </div>

      {error && <ErrorMessage message={error} />}

      <section className="surface-card border-(--border-subtle) p-5">
        <h2 className="text-base font-bold">都道府県</h2>
        <p className="mt-1 text-sm text-(--color-foreground)/60">
          選ばなければ全国が対象です。取り込み済みの都道府県だけが結果に出ます。
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {PREFECTURES.map((pref) => {
            const active = criteria.prefectures.includes(pref);
            return (
              <button
                key={pref}
                type="button"
                onClick={() => update("prefectures", toggle(criteria.prefectures, pref))}
                className={
                  active
                    ? "badge-blue px-3 py-1 text-sm"
                    : "rounded-[6px] border border-(--border-subtle) px-3 py-1 text-sm hover:bg-(--color-surface-muted)"
                }
              >
                {pref}
              </button>
            );
          })}
        </div>
      </section>

      <section className="surface-card border-(--border-subtle) p-5">
        <h2 className="text-base font-bold">施設の条件</h2>

        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">客室数</span>
            <span className="flex items-center gap-2">
              <input
                type="number"
                min={0}
                className="input w-24"
                value={criteria.room_count_min}
                onChange={(e) => update("room_count_min", toNumberOrNull(e.target.value) ?? 1)}
              />
              室以上
              <input
                type="number"
                min={1}
                className="input w-24"
                placeholder="上限なし"
                value={criteria.room_count_max ?? ""}
                onChange={(e) => update("room_count_max", toNumberOrNull(e.target.value))}
              />
              室未満
            </span>
            <span className="text-xs text-(--color-foreground)/60">
              客室数が読み取れていない施設は結果に含めません。
            </span>
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">クチコミ点数</span>
            <span className="flex items-center gap-2">
              <input
                type="number"
                step="0.1"
                min={0}
                max={5}
                className="input w-24"
                placeholder="下限"
                value={criteria.review_min ?? ""}
                onChange={(e) => update("review_min", toNumberOrNull(e.target.value))}
              />
              以上
              <input
                type="number"
                step="0.1"
                min={0}
                max={5}
                className="input w-24"
                placeholder="上限"
                value={criteria.review_max ?? ""}
                onChange={(e) => update("review_max", toNumberOrNull(e.target.value))}
              />
              未満
            </span>
            <label className="flex items-center gap-2 text-xs text-(--color-foreground)/60">
              <input
                type="checkbox"
                checked={criteria.include_unrated}
                onChange={(e) => update("include_unrated", e.target.checked)}
              />
              クチコミがまだ無い施設も含める
            </label>
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">クチコミ件数</span>
            <span className="flex items-center gap-2">
              <input
                type="number"
                min={0}
                className="input w-28"
                placeholder="指定なし"
                value={criteria.min_review_count ?? ""}
                onChange={(e) => update("min_review_count", toNumberOrNull(e.target.value))}
              />
              件以上
            </span>
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">掲載写真の枚数</span>
            <span className="flex items-center gap-2">
              <input
                type="number"
                min={0}
                className="input w-28"
                placeholder="指定なし"
                value={criteria.max_photo_count ?? ""}
                onChange={(e) => update("max_photo_count", toNumberOrNull(e.target.value))}
              />
              枚未満
            </span>
            <span className="text-xs text-(--color-foreground)/60">
              写真が少ない施設＝撮影提案の対象。
            </span>
          </label>
        </div>

        <div className="mt-5">
          <span className="text-sm font-medium">施設カテゴリー（推定）</span>
          <div className="mt-2 flex flex-wrap gap-2">
            {CATEGORIES.map((category) => {
              const active = criteria.categories.includes(category.value);
              return (
                <button
                  key={category.value}
                  type="button"
                  onClick={() => update("categories", toggle(criteria.categories, category.value))}
                  className={
                    active
                      ? "badge-blue px-3 py-1 text-sm"
                      : "rounded-[6px] border border-(--border-subtle) px-3 py-1 text-sm hover:bg-(--color-surface-muted)"
                  }
                >
                  {category.label}
                </button>
              );
            })}
          </div>
        </div>

        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">楽天カスタマイズページ</span>
            <select
              className="input"
              value={criteria.custom_page ?? ""}
              onChange={(e) => update("custom_page", e.target.value || null)}
            >
              <option value="">問わない</option>
              <option value="not_published">未作成のみ</option>
              <option value="published">公開ありのみ</option>
            </select>
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">チェックイン機</span>
            <select
              className="input"
              value={criteria.check_in_machine ?? ""}
              onChange={(e) => update("check_in_machine", e.target.value || null)}
            >
              <option value="">問わない</option>
              <option value="yes">導入ありのみ（確認済み）</option>
              <option value="unknown">未確認のみ</option>
              <option value="no">導入なしのみ（確認済み）</option>
            </select>
            <span className="text-xs text-(--color-foreground)/60">
              楽天のページにはほとんど書かれていないため、既定では大半が「未確認」です。
              WEB検索で確かめた施設だけが「導入あり／なし」になります。
            </span>
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">温泉</span>
            <select
              className="input"
              value={criteria.has_onsen === null ? "" : criteria.has_onsen ? "yes" : "no"}
              onChange={(e) =>
                update("has_onsen", e.target.value === "" ? null : e.target.value === "yes")
              }
            >
              <option value="">問わない</option>
              <option value="yes">温泉ありのみ</option>
              <option value="no">温泉なしのみ</option>
            </select>
          </label>
        </div>

        <label className="mt-4 flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={criteria.exclude_chains}
            onChange={(e) => update("exclude_chains", e.target.checked)}
          />
          チェーン系（アパ・東横イン・ルートイン等）を除く
        </label>
      </section>

      <section className="surface-card border-(--border-subtle) p-5">
        <h2 className="text-base font-bold">CRMとの突合</h2>
        <p className="mt-1 text-sm text-(--color-foreground)/60">
          ここを指定すると書き出しに時間がかかります（Notionを読むため）。
          件数の確認では使えません。
        </p>

        <label className="mt-3 flex flex-col gap-1 text-sm sm:w-72">
          <span className="font-medium">取引の状態</span>
          <select
            className="input"
            value={criteria.crm_filter}
            onChange={(e) => update("crm_filter", e.target.value)}
          >
            <option value="any">問わない</option>
            <option value="new_only">未取引の可能性のみ（新規開拓）</option>
            <option value="existing_only">既存取引先のみ（アップセル）</option>
          </select>
          <span className="text-xs text-(--color-foreground)/60">
            突合は<strong>施設名だけ</strong>で行います。CRMに運営会社名で登録されている
            既存顧客は「未取引の可能性」に混ざります。架電前に取引先名を確認してください。
          </span>
        </label>

        <div className="mt-4">
          <span className="text-sm font-medium">この商材を提案済みの施設を除く</span>
          <div className="mt-2 flex flex-wrap gap-2">
            {PRODUCTS.map((product) => {
              const active = criteria.exclude_proposed_services.includes(product);
              return (
                <button
                  key={product}
                  type="button"
                  onClick={() =>
                    update(
                      "exclude_proposed_services",
                      toggle(criteria.exclude_proposed_services, product)
                    )
                  }
                  className={
                    active
                      ? "badge-blue px-3 py-1 text-sm"
                      : "rounded-[6px] border border-(--border-subtle) px-3 py-1 text-sm hover:bg-(--color-surface-muted)"
                  }
                >
                  {product}
                </button>
              );
            })}
          </div>
        </div>
      </section>

      <section className="surface-card border-(--border-subtle) p-5">
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className="btn-ghost"
            onClick={handlePreview}
            disabled={previewing || usesCrmCondition}
          >
            {previewing ? "数えています..." : "件数を見る"}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={handleExport}
            disabled={exporting}
          >
            {exporting ? "書き出しています..." : "CSVをダウンロード"}
          </button>
          {usesCrmCondition && (
            <span className="text-sm text-(--color-foreground)/60">
              CRMの条件を指定しているため、件数の確認は使えません。そのまま書き出してください。
            </span>
          )}
        </div>

        {exportNote && (
          <p className="mt-4 text-sm text-(--color-foreground)/80">{exportNote}</p>
        )}

        {preview && (
          <div className="mt-5">
            <p className="text-sm">
              条件に当たる施設：<strong className="text-lg">{preview.total}</strong> 件
              {preview.truncated && (
                <span className="ml-2 text-(--color-foreground)/60">
                  （下の表は先頭 {preview.rows.length} 件）
                </span>
              )}
            </p>
            <p className="mt-1 text-xs text-(--color-foreground)/60">
              この一覧はCRMと突合していません。「CRM状態」は未突合と出ます。
            </p>
            <div className="mt-3 overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    {preview.headers.map((header) => (
                      <th key={header}>{header}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row, index) => (
                    <tr key={`${row[0]}-${index}`}>
                      {row.map((cell, cellIndex) => (
                        <td key={cellIndex}>{cell}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
