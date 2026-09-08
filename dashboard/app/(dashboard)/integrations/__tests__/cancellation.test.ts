import React from "react";
import { afterEach, expect, it, vi } from "vitest";
import IntegrationDiagnostics from "../IntegrationDiagnostics";
const hooks = vi.hoisted(() => ({ updates: [] as unknown[], cleanup: undefined as (() => void) | undefined }));
vi.mock("react", async (importOriginal) => {
  const original = await importOriginal<typeof import("react")>();
  return { ...original, useState: (value: unknown) => [value, (next: unknown) => hooks.updates.push(typeof next === "function" ? next({}) : next)], useRef: (current: unknown) => ({ current }), useEffect: (effect: () => (() => void)) => { hooks.cleanup = effect(); } };
});
afterEach(() => vi.unstubAllGlobals());
it("画面離脱で送信中の待機を中断し、後続の診断を起動しない", async () => {
  vi.stubGlobal("React", React);
  let signal: AbortSignal | undefined;
  const fetchMock = vi.fn((_url: string, options: RequestInit) => {
    signal = options.signal as AbortSignal;
    return new Promise((_resolve, reject) => signal!.addEventListener("abort", () => reject(new Error("中断")), { once: true }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const root = IntegrationDiagnostics();
  const elements: React.ReactNode[] = [root];
  let start: (() => void) | undefined;
  while (elements.length) {
    const element = elements.shift();
    if (!React.isValidElement<{ children?: React.ReactNode; onClick?: () => void }>(element)) continue;
    if (element.type === "button") { start = element.props.onClick; break; }
    elements.push(...React.Children.toArray(element.props.children));
  }
  expect(start).toBeTypeOf("function");
  start!();
  expect(fetchMock).toHaveBeenCalledTimes(1);
  hooks.cleanup!();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(signal?.aborted).toBe(true);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it.each([401, 403])("HTTP %sでは固定案内を出して後続診断を止める", async (status) => {
  hooks.updates = [];
  vi.stubGlobal("React", React);
  const fetchMock = vi.fn().mockResolvedValue(new Response('{"detail":"SECRET"}', { status }));
  vi.stubGlobal("fetch", fetchMock);
  const elements: React.ReactNode[] = [IntegrationDiagnostics()];
  while (elements.length) {
    const element = elements.shift();
    if (!React.isValidElement<{ children?: React.ReactNode; onClick?: () => void }>(element)) continue;
    if (element.type === "button") { element.props.onClick!(); break; }
    elements.push(...React.Children.toArray(element.props.children));
  }
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const rendered = JSON.stringify(hooks.updates);
  expect(rendered).toContain(status === 401 ? "再ログイン" : "管理者権限");
  expect(rendered).toContain("後続の診断を停止");
  expect(rendered).not.toContain("SECRET");
});
