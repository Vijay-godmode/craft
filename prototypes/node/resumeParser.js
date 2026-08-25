// Minimal resume parser for Node.js (text-based)
function parseResumeText(text){
  if(!text) return {};
  const lines = text.split(/\r?\n/).map(l=>l.trim()).filter(Boolean);
  const sections = { header: [] };
  let current = 'header';
  for(const line of lines){
    const low = line.toLowerCase();
    if (low.startsWith('experience')||low.startsWith('work')){ current='experience'; sections[current]=[]; continue; }
    if (low.startsWith('education')){ current='education'; sections[current]=[]; continue; }
    if (low.startsWith('skills')){ current='skills'; sections[current]=[]; continue; }
    sections[current].push(line);
  }
  for(const k of Object.keys(sections)) sections[k]=sections[k].join('\n');
  return sections;
}

if(require.main===module){
  console.log(parseResumeText('Name\nSkills\nPython, Node.js\nExperience\nEngineer at Z'));
}

module.exports = { parseResumeText };
