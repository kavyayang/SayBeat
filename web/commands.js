// Confirmed instruction text only. Story/lyric quotes never execute commands.
export function classifyInstruction(text) {
  const ordinal=token=>({'一':0,'二':1,'三':2,'四':3,'1':0,'2':1,'3':2,'4':3})[token];
  if (/['"“”‘’「」《》]|歌词里|原话|引用|这句话|不是|不对|说错|更正|第[一二三四1-4](?:句|行).*和.*第[一二三四1-4](?:句|行)|不要.{0,8}(锁|解锁)|别.{0,8}(锁|解锁)|不.{0,4}(锁定|解锁)|先别/.test(text)) return {kind:'clarify',index:null};
  if (/(只|仅|单独).{0,8}(贝斯|bass|鼓点|鼓轨|人声轨|伴奏轨)/i.test(text)) return {kind:'unsupported',index:null};
  const all=[...text.matchAll(/第([一二三四1-4])(?:句|行)/g)].map(match=>ordinal(match[1]));
  if(text.includes('整首')||text.includes('全部歌词'))return {kind:'revise',index:null};
  const target=/(?:改写?|重写|调整|润色|替换)[^，。；]{0,8}第([一二三四1-4])(?:句|行)|第([一二三四1-4])(?:句|行)[^，。；]{0,12}(?:改|重写|调整|润色|换|更)/.exec(text);
  if(/^(?:请|帮我|把)?(?:锁住|锁定|解锁|取消锁定)/.test(text)||/第[一二三四1-4](?:句|行)[^，。；]{0,6}(?:锁住|锁定|解锁)/.test(text)){
    if(all.length!==1)return {kind:'clarify',index:null};
    return {kind:/解锁|取消锁定/.test(text)?'unlock':'lock',index:all[0]};
  }
  const index=target?ordinal(target[1]||target[2]):all.length===1?all[0]:null;
  if(all.length>1&&!target || all.length&&index===null)return {kind:'clarify',index:null};
  return {kind:'revise',index};
}
