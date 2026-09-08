// Vercel本番の TOKEN_ENCRYPTION_KEY を、値を露出させずに回収する（2026-09-08）。
//
// ★ 1プロセスで完結させる理由
//   回収用のRSA秘密鍵をディスクに置かないため。工程を分けると、どこかに書き出す
//   必要が出てしまう。このスクリプトは生成・デプロイ・取得・復号・保存・後片付けを
//   ひと続きに行い、終了と同時に秘密鍵が消える。
//
// やること
//   1. 事前確認   いまの本番デプロイIDとaliasを控える（読み取りのみ）
//   2. 鍵生成     RSA-3072をメモリ上に作り、公開鍵だけを書き出す
//   3. 仮置き場   一時ディレクトリに seal.mjs / vercel.json / 公開鍵 / project.json
//   4. デプロイ   vercel deploy --prod --skip-domain --yes（★本番ドメインは動かさない）
//   5. 検証       aliasが元のデプロイに付いたままか。新デプロイが未認証で開けないか
//   6. 取得       vercel curl で /recovery.json を取り、メモリ上で復号
//   7. 照合       既存のGmail暗号文を実際に復号できるか
//   8. 保存       macOSキーチェーンへ（標準入力で渡すので ps から見えない）
//   9. 後片付け   今回作ったデプロイだけ削除。一時ディレクトリも消す
//
// 使い方: node scripts/key_recovery/recover.mjs

import { execFileSync, spawnSync } from 'node:child_process';
import { constants, generateKeyPairSync, privateDecrypt } from 'node:crypto';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..', '..');
const DASHBOARD = join(REPO, 'dashboard');
const KEYCHAIN_SERVICE = 'crm-sfa-integration-TOKEN_ENCRYPTION_KEY';
const KEYCHAIN_ACCOUNT = 'production';

const say = (step, msg) => console.log(`[${step}] ${msg}`);
const die = (msg) => { throw new Error(msg); };

// stdoutだけを取る。失敗時は例外。
function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { encoding: 'utf8', ...opts });
  if (r.error) die(`${cmd} の起動に失敗: ${r.error.message}`);
  if (r.status !== 0) die(`${cmd} ${args[0]} が失敗 (exit ${r.status})\n${(r.stderr || '').slice(-800)}`);
  return r.stdout;
}

// vercel inspect の出力から id と alias を拾う
function inspect(url) {
  const out = spawnSync('vercel', ['inspect', url], { cwd: DASHBOARD, encoding: 'utf8' });
  const text = `${out.stdout}\n${out.stderr}`;
  const id = (text.match(/\bdpl_[A-Za-z0-9]+/) || [])[0] || '';
  const aliases = [...text.matchAll(/╶\s+(https:\/\/\S+)/g)].map((m) => m[1]);
  return { id, aliases, raw: text };
}

let tmp = null;
let createdUrl = null;
let secret = null;

try {
  // ── 1. 事前確認 ─────────────────────────────────────────────
  const project = JSON.parse(readFileSync(join(DASHBOARD, '.vercel', 'project.json'), 'utf8'));
  say(1, `プロジェクト ${project.projectName} (${project.projectId})`);

  const prodList = run('vercel', ['ls', '--prod'], { cwd: DASHBOARD });
  const prodUrl = (prodList.match(/https:\/\/\S+\.vercel\.app/) || [])[0]
    || die('現在の本番デプロイURLを取得できません');
  const before = inspect(prodUrl);
  before.id || die('現在の本番デプロイIDを取得できません');
  before.aliases.length || die('現在のaliasを取得できません');
  say(1, `いまの本番 ${before.id}`);
  before.aliases.forEach((a) => say(1, `  alias ${a}`));

  // ── 2. 回収用の鍵をメモリ上に作る ───────────────────────────
  const { publicKey, privateKey } = generateKeyPairSync('rsa', { modulusLength: 3072 });
  const publicPem = publicKey.export({ type: 'spki', format: 'pem' });
  say(2, 'RSA-3072 を生成（秘密鍵はこのプロセスのメモリだけ）');

  // ── 3. 仮置き場 ─────────────────────────────────────────────
  tmp = mkdtempSync(join(tmpdir(), 'crm-key-recovery-'));
  copyFileSync(join(HERE, 'seal.mjs'), join(tmp, 'seal.mjs'));
  copyFileSync(join(HERE, 'vercel.template.json'), join(tmp, 'vercel.json'));
  writeFileSync(join(tmp, 'recovery-public.pem'), publicPem, { mode: 0o600 });
  mkdirSync(join(tmp, '.vercel'), { recursive: true });
  writeFileSync(
    join(tmp, '.vercel', 'project.json'),
    JSON.stringify({ projectId: project.projectId, orgId: project.orgId }),
    { mode: 0o600 },
  );
  say(3, `仮置き場 ${tmp}（アプリのコードも .env も入れていない）`);

  // ── 4. デプロイ（本番ドメインは動かさない）────────────────────
  say(4, 'vercel deploy --prod --skip-domain --yes を実行します');
  const deployOut = run('vercel', ['deploy', '--prod', '--skip-domain', '--yes'], { cwd: tmp });
  createdUrl = (deployOut.match(/https:\/\/\S+\.vercel\.app/g) || []).pop()
    || die('作成したデプロイのURLを取得できません');
  say(4, `作成 ${createdUrl}`);

  // ── 5. 本番が動いていないことを確かめる ──────────────────────
  const after = inspect(prodUrl);
  if (after.id !== before.id) die(`本番デプロイIDが変わりました: ${before.id} → ${after.id}`);
  const lost = before.aliases.filter((a) => !after.aliases.includes(a));
  if (lost.length) die(`aliasが外れました: ${lost.join(', ')}`);
  const mine = inspect(createdUrl);
  const stolen = mine.aliases.filter((a) => before.aliases.includes(a));
  if (stolen.length) die(`新しいデプロイが本番aliasを取りました: ${stolen.join(', ')}`);
  say(5, '本番デプロイIDとaliasは元のまま。新デプロイは本番ドメインを持っていない');

  const anon = await fetch(`${createdUrl}/recovery.json`, { redirect: 'manual' });
  if (anon.status === 200) die(`未認証で開けてしまいます (HTTP ${anon.status})。回収せず中止`);
  say(5, `未認証アクセスは拒否 (HTTP ${anon.status})`);

  // ── 6. 取得して復号 ─────────────────────────────────────────
  const body = run('vercel', ['curl', `${createdUrl}/recovery.json`], { cwd: DASHBOARD });
  const envelope = JSON.parse(body.slice(body.indexOf('{')));
  if (envelope.projectId !== project.projectId) die('封筒のプロジェクトIDが一致しません');
  secret = privateDecrypt(
    {
      key: privateKey,
      padding: constants.RSA_PKCS1_OAEP_PADDING,
      oaepHash: 'sha256',
      oaepLabel: Buffer.from(`crm-sfa-key-recovery:v1:${project.projectId}`),
    },
    Buffer.from(envelope.ciphertext, 'base64'),
  ).toString('utf8');
  if (!/^[0-9a-fA-F]{64}$/.test(secret)) die('取り出した値が64桁hexではありません');
  say(6, '復号できました（値は表示しません）');

  // ── 7. 既存の暗号文で照合 ───────────────────────────────────
  const verify = spawnSync(join(REPO, '.venv', 'bin', 'python'),
    [join(HERE, 'verify_key.py')], { input: secret, encoding: 'utf8', cwd: REPO });
  say(7, (verify.stdout || verify.stderr || '').trim());
  if (verify.status !== 0) die('既存の暗号文を復号できませんでした。保存せず中止します');

  // ── 8. キーチェーンへ（標準入力なので ps から見えない）────────
  spawnSync('security', ['delete-generic-password', '-s', KEYCHAIN_SERVICE], { stdio: 'ignore' });
  const save = spawnSync('security',
    ['add-generic-password', '-a', KEYCHAIN_ACCOUNT, '-s', KEYCHAIN_SERVICE, '-U', '-w'],
    { input: `${secret}\n${secret}\n`, encoding: 'utf8' });
  if (save.status !== 0) die(`キーチェーン保存に失敗: ${(save.stderr || '').trim()}`);
  const back = spawnSync('security', ['find-generic-password', '-s', KEYCHAIN_SERVICE, '-w'],
    { encoding: 'utf8' });
  if (back.stdout.trim() !== secret) die('キーチェーンの読み戻しが一致しません');
  say(8, `キーチェーンへ保存（サービス名 ${KEYCHAIN_SERVICE}）`);

  console.log('\n── 完了 ────────────────────────────────────────────');
  console.log('  取り出しは成功しました。値はこの画面にもログにも出していません。');
  console.log(`  取り出す:  security find-generic-password -s ${KEYCHAIN_SERVICE} -w`);
} finally {
  if (secret) secret = null;
  if (createdUrl) {
    const r = spawnSync('vercel', ['remove', createdUrl, '--yes'], { cwd: DASHBOARD, encoding: 'utf8' });
    say(9, r.status === 0 ? `回収用デプロイを削除 ${createdUrl}`
      : `★手で消してください: vercel remove ${createdUrl} --yes`);
  }
  if (tmp) { rmSync(tmp, { recursive: true, force: true }); say(9, '仮置き場を削除'); }
}
