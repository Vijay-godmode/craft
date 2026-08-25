const { extractSkillsFromText } = require('./skillExtractor');
const { parseResumeText } = require('./resumeParser');

function scoreResumeAgainstPosting(postingText, resumeText){
  const postingSkills = new Set(extractSkillsFromText(postingText,100));
  const sections = parseResumeText(resumeText);
  const resumeFull = Object.values(sections).join('\n');
  const resumeSkills = new Set(extractSkillsFromText(resumeFull,200));
  const matched = [...postingSkills].filter(x=>resumeSkills.has(x));
  const score = postingSkills.size ? Math.round(100 * matched.length / postingSkills.size) : 0;
  const suggestions = [...postingSkills].filter(x=>!resumeSkills.has(x)).slice(0,20);
  return { postingSkillCount: postingSkills.size, resumeSkillCount: resumeSkills.size, matchedCount: matched.length, scorePercent: score, matched, suggestions };
}

if(require.main===module){
  const posting = 'Senior Python Developer with Django, Flask, AWS, Docker, Kubernetes';
  const resume = 'Skills\nPython, Flask, Docker\nExperience\nDeveloper at Y';
  console.log(scoreResumeAgainstPosting(posting,resume));
}

module.exports = { scoreResumeAgainstPosting };
