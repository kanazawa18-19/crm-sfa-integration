// 移行時の本人宛1通確認。業務処理・プロパティ書込み・トリガー操作を行わない。
function verifyOwnerNotification() {
  var from = 'daizuhakkou0619@gmail.com';
  var to = Session.getEffectiveUser().getEmail();
  if (to !== 'kanazawa@cnctor.jp') throw new Error('通知先が今回確認した本人宛と一致しません');
  if (GmailApp.getAliases().indexOf(from) < 0) throw new Error('個人送信元が実行アカウントに登録されていません');
  GmailApp.sendEmail(to, '[試験] 通知送信元の確認 crm-name 20261022', '個人Gmailから会社宛ての通知確認です。業務処理は実行していません。', {from: from});
  return '送信API完了。受信側でFromと到達を別途確認してください。';
}
