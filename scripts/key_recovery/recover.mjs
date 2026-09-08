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

// ★ alias が実際にどのデプロイを指しているかを引く（2026-09-08 の失敗を受けて追加）。
//   `vercel inspect <デプロイ>` の Aliases 欄はデプロイ側の記録で、実際の指し先と
//   ずれることがある。判断は必ず「alias を inspect して出るデプロイID」で行う。
//   空文字は「どのデプロイも指していない＝宙に浮いている」。
function resolveAlias(aliasUrl) {
  const out = spawnSync('vercel', ['inspect', aliasUrl], { cwd: DASHBOARD, encoding: 'utf8' });
  return ((`${out.stdout}\n${out.stderr}`).match(/\bdpl_[A-Za-z0-9]+/) || [])[0] || '';
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
let primaryAlias = null;
let aliasesToRestore = [];
let beforeId = null;
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
  beforeId = before.id;
  say(1, `いまの本番 ${before.id}`);
  for (const a of before.aliases) say(1, `  alias ${a} → ${resolveAlias(a) || '(なし)'}`);
  // 主ドメイン = 一番短いalias。ここだけは絶対に動かさない。
  primaryAlias = [...before.aliases].sort((x, y) => x.length - y.length)[0];
  say(1, `主ドメイン ${primaryAlias}（これが動いたら中止する）`);
  aliasesToRestore = before.aliases;

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
  // ★ --skip-domain が守るのは主ドメインだけ（2026-09-08 実測）。
  //   チーム用の <プロジェクト>-<チーム>.vercel.app は、それでも新しい本番デプロイへ移る。
  //   なので「主ドメインが動いたら中止」「チーム別名は移る前提で最後に戻す」に分ける。
  //   前回はここで中止したせいで、削除後に別名が宙に浮いた（404）。
  const primaryNow = resolveAlias(primaryAlias);
  if (primaryNow !== before.id) {
    die(`主ドメインが動きました: ${primaryAlias} → ${primaryNow || '(なし)'}。回収せず中止`);
  }
  say(5, `主ドメインは元のまま ${primaryAlias} → ${before.id}`);
  for (const a of before.aliases) {
    if (a === primaryAlias) continue;
    const now = resolveAlias(a);
    if (now !== before.id) say(5, `  ${a} は一時的に移りました（最後に戻します）`);
  }

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

  // ★ 別名を元のデプロイへ戻す（2026-09-08 の失敗の再発防止）。
  //   デプロイを消すと、そこへ移っていた別名は宙に浮いて404になる。
  //   実際に指し先がずれているものだけを、控えておいた元のデプロイへ張り直す。
  if (beforeId && aliasesToRestore.length) {
    for (const a of aliasesToRestore) {
      const host = a.replace(/^https?:\/\//, '');
      if (resolveAlias(a) === beforeId) continue;
      const r = spawnSync('vercel', ['alias', 'set', beforeId, host],
        { cwd: DASHBOARD, encoding: 'utf8' });
      const fixed = resolveAlias(a) === beforeId;
      say(9, fixed ? `別名を戻しました ${host} → ${beforeId}`
        : `★手で戻してください: vercel alias set ${beforeId} ${host}\n    ${(r.stderr || '').trim().slice(-200)}`);
    }
  }
}
