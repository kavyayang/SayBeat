export function tapTempo(taps) {
  if(!Array.isArray(taps)||taps.length<4||taps.length>16)return null;
  const intervals=taps.slice(1).map((x,i)=>x-taps[i]);
  if(intervals.some(x=>!Number.isInteger(x)||x<300||x>1500))return null;
  const sorted=[...intervals].sort((a,b)=>a-b),mid=Math.floor(sorted.length/2),middle=sorted.length%2?sorted[mid]:(sorted[mid-1]+sorted[mid])/2;
  return {bpm:Math.round(60000/middle),taps_ms:taps.map(x=>x-taps[0])};
}
