// 待機用Vercelビルド専用。秘密値は返さず、このMacの公開鍵で包んだ暗号文だけを作る。
import { constants, createPublicKey, publicEncrypt } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const PROJECT = 'prj_xBiFEQcP7NlshpK6tyh4gSCQeamj';

export function seal(env, publicPem) {
  if (env.VERCEL_ENV !== 'production' || env.VERCEL_PROJECT_ID !== PROJECT) {
    throw new Error('対象環境が一致しません');
  }
  const secret = env.TOKEN_ENCRYPTION_KEY;
  if (!/^[0-9a-fA-F]{64}$/.test(secret ?? '')) {
    throw new Error('鍵の形式が不正です');
  }
  const key = createPublicKey(publicPem);
  if (key.asymmetricKeyType !== 'rsa' || key.asymmetricKeyDetails.modulusLength < 3072) {
    throw new Error('回収用公開鍵が不正です');
  }
  const plaintext = Buffer.from(secret, 'utf8');
  try {
    return {
      version: 1,
      projectId: PROJECT,
      algorithm: 'RSA-OAEP-SHA256',
      ciphertext: publicEncrypt({
        key,
        padding: constants.RSA_PKCS1_OAEP_PADDING,
        oaepHash: 'sha256',
        oaepLabel: Buffer.from(`crm-sfa-key-recovery:v1:${PROJECT}`),
      }, plaintext).toString('base64'),
    };
  } finally {
    plaintext.fill(0);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const envelope = seal(process.env, readFileSync('recovery-public.pem', 'utf8'));
    mkdirSync('public', { recursive: true });
    writeFileSync('public/recovery.json', JSON.stringify(envelope), { flag: 'wx' });
    console.log('回収用の暗号文を作成しました');
  } catch {
    // 例外本文にはライブラリ入力が含まれ得るので固定文だけを出す。
    console.error('回収用ビルドに失敗しました。秘密値は出力していません。');
    process.exitCode = 1;
  }
}
