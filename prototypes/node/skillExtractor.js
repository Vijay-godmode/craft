// Minimal skill extractor prototype for Node.js
const fs = require('fs');

function extractSkillsFromText(text, topN=30) {
  if (!text) return [];
  const words = text.replace(/[\n\r]+/g, ' ').split(/[^A-Za-z#+.\-]+/).map(w=>w.trim().toLowerCase()).filter(Boolean);
  const stop = new Set(['and','or','the','a','an','with','to','of','in','for','on','by','as','at']);
  const freq = {};
  for (const w of words){
    if (w.length<2 || stop.has(w) || /^\d+$/.test(w)) continue;
    freq[w] = (freq[w]||0)+1;
  }
  const arr = Object.entries(freq).sort((a,b)=>b[1]-a[1]).slice(0,topN).map(x=>x[0]);
  return arr;
}

if (require.main===module){
  const sample = 'Senior Python Developer with experience in Django, Flask, REST, AWS, Docker, Kubernetes, SQL.';
  console.log(extractSkillsFromText(sample));
}

module.exports = { extractSkillsFromText };
