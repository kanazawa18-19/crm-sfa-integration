"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");

test("空キーを送らず、一行が失敗しても残りの行を送る", () => {
  const sent=[];
  const sheet={getName:()=>"チェーン", getParent:()=>({getSpreadsheetTimeZone:()=>"Asia/Tokyo"}),
    getLastColumn:()=>2, getRange:(row)=>({getValues:()=>row===1 ? [["名前","同期キー"]] : [["業務値",row===2 ? "" : "key-"+row]]})};
  const range={getSheet:()=>sheet,getRow:()=>2,getNumRows:()=>3,getColumn:()=>1,getNumColumns:()=>2};
  const context=vm.createContext({BUSINESS_SHEET_NAMES:[],DELETE_FLAG_COLUMN:"削除フラグ",
    isSyncManagedSheet:()=>true, dataRowNumbersInRange:()=>[2,3,4],
    rowValuesToRecord:(headers,values)=>({"同期キー":values[1]}),
    buildEditPayload:(sheet,row,editedAt,values)=>({row,values}),
    PropertiesService:{getScriptProperties:()=>({getProperty:()=>null})},
    isWithinSyncWriteWindow:()=>false});
  vm.runInContext(fs.readFileSync(__dirname+"/onEdit.js","utf8"),context);
  context.postToWebhook_=payload=>{sent.push(payload.row);if(payload.row===3)throw new Error("private");};
  assert.throws(()=>context.onEditSync({range}),/一部の行/);
  assert.deepEqual(sent,[3,4]);
});
