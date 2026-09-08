// ダミーの鍵で、復号・宛先の制限・ログへの非露出を確認する。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { constants, generateKeyPairSync, privateDecrypt, randomBytes } from 'node:crypto';
import { seal } from './seal.mjs';

const pair = generateKeyPairSync('rsa', { modulusLength: 3072 });
const publicPem = pair.publicKey.export({ type: 'spki', format: 'pem' });
const env = {
  VERCEL_ENV: 'production',
  VERCEL_PROJECT_ID: 'prj_xBiFEQcP7NlshpK6tyh4gSCQeamj',
  TOKEN_ENCRYPTION_KEY: randomBytes(32).toString('hex'),
};
const decryptOptions = {
  key: pair.privateKey,
  padding: constants.RSA_PKCS1_OAEP_PADDING,
  oaepHash: 'sha256',
  oaepLabel: Buffer.from(`crm-sfa-key-recovery:v1:${env.VERCEL_PROJECT_ID}`),
};

test('回収用秘密鍵だけで元の鍵を開ける。出力に平文を含めない', () => {
  const result = seal(env, publicPem);
  assert.equal(privateDecrypt(decryptOptions, Buffer.from(result.ciphertext, 'base64')).toString(), env.TOKEN_ENCRYPTION_KEY);
  assert.ok(!JSON.stringify(result).includes(env.TOKEN_ENCRYPTION_KEY));
  const wrongPair = generateKeyPairSync('rsa', { modulusLength: 3072 });
  assert.throws(() => privateDecrypt({ ...decryptOptions, key: wrongPair.privateKey }, Buffer.from(result.ciphertext, 'base64')));
});

test('環境・プロジェクト・鍵形式が違うと出力しない', () => {
  for (const override of [
    { VERCEL_ENV: 'preview' },
    { VERCEL_PROJECT_ID: '別プロジェクト' },
    { TOKEN_ENCRYPTION_KEY: '' },
    { TOKEN_ENCRYPTION_KEY: 'x'.repeat(64) },
  ]) assert.throws(() => seal({ ...env, ...override }, publicPem));
});

test('改ざんした暗号文と弱い公開鍵は拒否する', () => {
  const ciphertext = Buffer.from(seal(env, publicPem).ciphertext, 'base64');
  ciphertext[0] ^= 1;
  assert.throws(() => privateDecrypt(decryptOptions, ciphertext));
  const weak = generateKeyPairSync('rsa', { modulusLength: 2048 });
  assert.throws(() => seal(env, weak.publicKey.export({ type: 'spki', format: 'pem' })));
});
