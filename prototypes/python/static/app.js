/* CareerCraft client UI. API and user supplied content is rendered with DOM text nodes. */
(function () {
  'use strict';

  const page = document.body.dataset.page || '';
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const $$ = (selector, parent = document) => Array.from(parent.querySelectorAll(selector));
  const JOB_WATCH_KEY = 'careercraft_job_watch_config';
  const JOB_SEEN_KEY = 'careercraft_seen_jobs';
  const JOB_SEEN_INITIALIZED_KEY = 'careercraft_seen_jobs_initialized';
  const JOB_WATCH_INTERVAL_MS = 6 * 60 * 60 * 1000;
  let builderAnalysis = null;
  let builderResume = null;
  let builderJobId = null;
  let jobWatchTimer = null;
  let profileTemplateActive = false;

  function csrfToken() {
    return document.body.dataset.csrfToken || '';
  }

  function updateCsrfToken(value) {
    if (value) document.body.dataset.csrfToken = String(value);
  }

  async function api(path, options = {}) {
    const config = { ...options, headers: { ...(options.headers || {}) } };
    const method = String(config.method || 'GET').toUpperCase();
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) && csrfToken()) config.headers['X-CSRF-Token'] = csrfToken();
    if (config.body && !(config.body instanceof FormData) && typeof config.body !== 'string') {
      config.headers['Content-Type'] = 'application/json';
      config.body = JSON.stringify(config.body);
    }
    const response = await fetch(path, config);
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('application/json') ? await response.json() : null;
    if (!response.ok) {
      if (response.status === 401 && !['sign-in', 'sign-up'].includes(page)) {
        window.location.href = `/sign-in?next=${encodeURIComponent(window.location.pathname)}`;
      }
      throw new Error((payload && payload.error) || 'Something went wrong. Please try again.');
    }
    return payload;
  }

  function node(tag, options = {}, children = []) {
    const element = document.createElement(tag);
    Object.entries(options).forEach(([key, value]) => {
      if (value === undefined || value === null) return;
      if (key === 'className') element.className = value;
      else if (key === 'text') element.textContent = String(value);
      else if (key === 'htmlFor') element.htmlFor = String(value);
      else if (key === 'value') element.value = String(value);
      else if (key === 'checked' || key === 'disabled' || key === 'selected' || key === 'hidden') element[key] = Boolean(value);
      else element.setAttribute(key, String(value));
    });
    children.flat().filter(Boolean).forEach((child) => element.append(child.nodeType ? child : document.createTextNode(String(child))));
    return element;
  }

  function clear(element) {
    while (element && element.firstChild) element.removeChild(element.firstChild);
  }

  function storageJson(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key) || ''); } catch (_) { return fallback; }
  }

  function toast(message, type = 'success') {
    const region = $('#toastRegion');
    if (!region) return;
    const item = node('div', { className: `toast ${type}`, text: message });
    region.append(item);
    window.setTimeout(() => item.remove(), 4400);
  }

  function formatDate(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' }).format(date);
  }

  function titleCase(value) {
    return String(value || '').replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function createChip(label, kind = '') {
    return node('span', { className: `chip ${kind}`.trim(), text: label });
  }

  function emptyState(title, copy, actionLabel, href) {
    const children = [node('span', { className: 'empty-icon', text: 'J' }), node('h3', { text: title }), node('p', { text: copy })];
    if (actionLabel && href) children.push(node('a', { className: 'button button-secondary', href, text: actionLabel }));
    return node('div', { className: 'empty-state' }, children);
  }

  function notificationPermission() {
    if (!('Notification' in window)) {
      toast('Browser notifications are not available here.', 'warning');
      return;
    }
    if (Notification.permission === 'granted') {
      toast('Browser job alerts are already enabled.');
      return;
    }
    Notification.requestPermission().then((permission) => {
      if (permission === 'granted') toast('Browser job alerts enabled while this workspace is open.');
      else toast('Notifications were not enabled. You can still review the Jobs inbox anytime.', 'warning');
    });
  }

  function trackNewJobs(jobs) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    const known = new Set(storageJson(JOB_SEEN_KEY, []));
    const initialized = localStorage.getItem(JOB_SEEN_INITIALIZED_KEY) === 'true';
    const newJobs = jobs.filter((job) => job.status === 'new' && !known.has(job.id));
    const ids = jobs.map((job) => job.id);
    localStorage.setItem(JOB_SEEN_KEY, JSON.stringify(Array.from(new Set([...known, ...ids])).slice(-300)));
    localStorage.setItem(JOB_SEEN_INITIALIZED_KEY, 'true');
    if (initialized && newJobs.length) {
      const notification = new Notification('CareerCraft: new QA roles', { body: `${newJobs.length} role${newJobs.length === 1 ? '' : 's'} is ready for review.` });
      notification.onclick = () => { window.focus(); window.location.href = '/jobs'; };
    }
  }

  function storeBuilderDraft(job) {
    localStorage.setItem('careercraft_builder_draft', JSON.stringify({
      id: job.id,
      title: job.title || '',
      company: job.company || '',
      source_url: job.source_url || '',
      description: job.description || '',
    }));
  }

  function jobMeta(job) {
    return [job.company, job.location, job.job_type].filter(Boolean).join(' / ') || 'Job details pending';
  }

  function createJobCard(job, compact = false) {
    const analysis = job.analysis || {};
    const card = node('article', { className: `job-card ${compact ? 'compact-job-card' : ''}` });
    const titleBlock = node('div', { className: 'job-card-title' }, [
      node('h3', { text: job.title || 'Untitled opportunity' }),
      node('p', { className: 'job-meta', text: jobMeta(job) }),
    ]);
    const score = node('div', { className: 'job-score' }, [
      node('strong', { text: `${analysis.job_match_score || 0}%` }),
      node('span', { text: 'profile match' }),
    ]);
    const headActions = node('div', { className: 'job-head-actions' }, [score]);
    if (job.status !== 'closed') {
      const close = node('button', { className: 'close-opportunity', type: 'button', title: 'Close this opportunity', 'aria-label': `Close ${job.title || 'opportunity'}`, text: 'X' });
      close.addEventListener('click', () => closeJob(job.id));
      headActions.append(close);
    }
    card.append(node('div', { className: 'job-card-head' }, [titleBlock, headActions]));
    const tags = node('div', { className: 'job-tags' }, [
      createChip(job.source || 'Manual', 'chip-neutral'),
      createChip(job.role_track || 'Test Engineer', 'chip-neutral'),
      createChip(titleCase(job.status || 'new'), `status-${job.status || 'new'}`),
    ]);
    if (job.is_product_company) tags.append(createChip('Product company board', 'chip-positive'));
    if (job.salary) tags.append(createChip('Salary disclosed', 'chip-positive'));
    if (job.qa_fit_score) tags.append(createChip(`QA fit ${job.qa_fit_score}`, 'chip-neutral'));
    (analysis.matched_skills || []).slice(0, 3).forEach((skill) => tags.append(createChip(skill, 'chip-positive')));
    card.append(tags);
    if (!compact) {
      const insight = analysis.missing_skills && analysis.missing_skills.length
        ? `Gap to review: ${analysis.missing_skills.slice(0, 3).join(', ')}`
        : 'No common QA gaps were detected from the available requirements.';
      card.append(node('p', { className: 'job-insight', text: insight }));
      if (job.company_signal || job.salary) card.append(node('p', { className: 'job-source-note', text: [job.company_signal, job.salary].filter(Boolean).join(' / ') }));
    }
    const actions = node('div', { className: 'job-actions' });
    const tailor = node('button', { className: 'text-button', type: 'button', text: 'Tailor resume' });
    tailor.addEventListener('click', () => { storeBuilderDraft(job); window.location.href = '/builder'; });
    actions.append(tailor);
    if (job.source_url) actions.append(node('a', { className: 'text-button', href: job.source_url, target: '_blank', rel: 'noopener noreferrer', text: 'Open original listing' }));
    if (job.status === 'new') {
      const approve = node('button', { className: 'button button-small button-primary', type: 'button', text: 'Approve' });
      approve.addEventListener('click', () => decideJob(job.id, 'approved'));
      const reject = node('button', { className: 'text-button danger', type: 'button', text: 'Reject' });
      reject.addEventListener('click', () => decideJob(job.id, 'rejected'));
      actions.append(approve, reject);
    } else if (job.status === 'approved') {
      const applied = node('button', { className: 'button button-small button-primary', type: 'button', text: 'Mark applied' });
      applied.addEventListener('click', () => decideJob(job.id, 'applied'));
      actions.append(applied);
    } else if (job.status === 'closed') {
      const reopen = node('button', { className: 'button button-small button-secondary', type: 'button', text: 'Restore to review' });
      reopen.addEventListener('click', () => reopenJob(job.id));
      actions.append(reopen);
    }
    card.append(actions);
    return card;
  }

  async function decideJob(id, status) {
    try {
      await api(`/api/jobs/${id}/decision`, { method: 'POST', body: { status } });
      toast(`Job marked ${status}.`);
      if (page === 'jobs') loadJobs(window.currentJobFilter || 'new');
      if (page === 'dashboard') loadDashboard();
    } catch (error) { toast(error.message, 'error'); }
  }

  async function closeJob(id) {
    try {
      await api(`/api/jobs/${id}/close`, { method: 'POST', body: {} });
      toast('Opportunity closed and removed from the active queue.');
      if (page === 'jobs') loadJobs(window.currentJobFilter || 'new');
      if (page === 'applications') loadApplications();
      if (page === 'dashboard') loadDashboard();
    } catch (error) { toast(error.message, 'error'); }
  }

  async function reopenJob(id) {
    try {
      await api(`/api/jobs/${id}/reopen`, { method: 'POST', body: {} });
      toast('Opportunity restored to the new review queue.');
      if (page === 'jobs') loadJobs(window.currentJobFilter || 'closed');
    } catch (error) { toast(error.message, 'error'); }
  }

  async function loadDashboard() {
    try {
      const data = await api('/api/dashboard');
      const completion = data.completion || {};
      $('#profileProgressLabel').textContent = `${completion.percent || 0}% complete`;
      $('#profileProgressBar').style.width = `${completion.percent || 0}%`;
      $('#profileProgressText').textContent = `${completion.complete || 0} of ${completion.total || 0} profile essentials complete`;
      $('#metricNewJobs').textContent = data.metrics.new_jobs;
      $('#metricApproved').textContent = data.metrics.approved;
      $('#metricApplied').textContent = data.metrics.applied;
      $('#metricVersions').textContent = data.metrics.resume_versions;
      const container = $('#dashboardJobs');
      clear(container);
      if (!data.latest_jobs.length) container.append(emptyState('No roles saved yet', 'Bring in a role from the Jobs inbox or paste a job description to begin.', 'Explore QA roles', '/jobs'));
      else data.latest_jobs.forEach((job) => container.append(createJobCard(job, true)));
      trackNewJobs(data.latest_jobs);
    } catch (error) { toast(error.message, 'error'); }
  }

  function splitLines(value) {
    return String(value || '').split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
  }

  function addEntry(templateId, containerId, data = {}) {
    const template = $(`#${templateId}`);
    const container = $(`#${containerId}`);
    if (!template || !container) return;
    const fragment = template.content.cloneNode(true);
    const entry = fragment.firstElementChild;
    $$('[data-field]', entry).forEach((input) => {
      const key = input.dataset.field;
      if (input.type === 'checkbox') input.checked = Boolean(data[key]);
      else if (key === 'bullets') input.value = (data[key] || []).join('\n');
      else input.value = data[key] || '';
    });
    $('.remove-entry', entry).addEventListener('click', () => { entry.remove(); updateProfileEntryHints(); });
    container.append(entry);
    updateProfileEntryHints();
  }

  function updateProfileEntryHints() {
    const empty = $('#experienceEmpty');
    if (empty) empty.hidden = $$('.experience-entry').length > 0;
  }

  function entryValues(selector) {
    return $$(selector).map((entry) => {
      const values = {};
      $$('[data-field]', entry).forEach((input) => {
        const key = input.dataset.field;
        values[key] = input.type === 'checkbox' ? input.checked : input.value.trim();
        if (key === 'bullets') values[key] = splitLines(input.value);
      });
      return values;
    }).filter((entry) => Object.values(entry).some((value) => Array.isArray(value) ? value.length : Boolean(value)));
  }

  function profileFromForm() {
    return {
      full_name: $('#fullName').value.trim(),
      headline: $('#headline').value.trim(),
      email: $('#email').value.trim(),
      phone: $('#phone').value.trim(),
      location: $('#location').value.trim(),
      linkedin_url: $('#linkedinUrl').value.trim(),
      portfolio_url: $('#portfolioUrl').value.trim(),
      summary: $('#summary').value.trim(),
      skills: splitLines($('#skills').value),
      experience: entryValues('.experience-entry'),
      education: entryValues('.education-entry'),
      certifications: splitLines($('#certifications').value),
      projects: entryValues('.project-entry'),
      is_starter_template: profileTemplateActive,
      template_name: profileTemplateActive ? 'QA / Test Engineer Starter' : '',
    };
  }

  function populateProfile(profile) {
    const map = { full_name: '#fullName', headline: '#headline', email: '#email', phone: '#phone', location: '#location', linkedin_url: '#linkedinUrl', portfolio_url: '#portfolioUrl', summary: '#summary' };
    Object.entries(map).forEach(([key, selector]) => { $(selector).value = profile[key] || ''; });
    $('#skills').value = (profile.skills || []).join(', ');
    $('#certifications').value = (profile.certifications || []).join('\n');
    clear($('#experienceList')); clear($('#educationList')); clear($('#projectList'));
    (profile.experience || []).forEach((item) => addEntry('experienceTemplate', 'experienceList', item));
    (profile.education || []).forEach((item) => addEntry('educationTemplate', 'educationList', item));
    (profile.projects || []).forEach((item) => addEntry('projectTemplate', 'projectList', item));
    updateProfileEntryHints();
  }

  function showProfileCompletion(completion) {
    const pill = $('#profileCompletionPill');
    if (pill) $('strong', pill).textContent = `${completion.percent || 0}%`;
  }

  function updateStarterNotice(profile, message) {
    const notice = $('#starterNotice');
    if (!notice) return;
    if (message) notice.textContent = message;
    else if (profile && profile.is_starter_template) notice.textContent = 'This editable starter is intentionally marked incomplete until every visible placeholder is replaced with your facts.';
    else notice.textContent = 'Your master profile is portable: export JSON for backup, or import a resume to create a reviewable draft.';
  }

  function applyProfileDraft(profile, completion, message) {
    profileTemplateActive = Boolean(profile && profile.is_starter_template);
    populateProfile(profile || {});
    showProfileCompletion(completion || {});
    updateStarterNotice(profile, message);
  }

  function renderAiResult(container, result, onApply) {
    if (!container) return;
    clear(container);
    container.hidden = false;
    const provider = node('p', { className: 'eyebrow', text: `${result.provider || 'Local review'} / ${result.mode || 'review'}` });
    const heading = node('h3', { text: 'Review results' });
    const summary = node('p', { className: 'ai-summary', text: result.summary || 'Review completed.' });
    container.append(provider, heading, summary);
    if (result.revised_text) {
      const revised = node('textarea', { className: 'ai-revised-text', rows: 5, readonly: 'readonly', 'aria-label': 'Suggested revised text', value: result.revised_text });
      container.append(node('h4', { text: 'Suggested revision' }), revised);
      if (onApply) {
        const apply = node('button', { className: 'button button-small button-secondary', type: 'button', text: 'Use this revision' });
        apply.addEventListener('click', () => onApply(result.revised_text));
        container.append(apply);
      }
    }
    const lists = node('div', { className: 'ai-lists' });
    if (result.suggestions && result.suggestions.length) {
      const list = node('ul');
      result.suggestions.forEach((item) => list.append(node('li', { text: `${item.suggestion || item.original || 'Review this wording.'}${item.reason ? ` (${item.reason})` : ''}` })));
      lists.append(node('div', {}, [node('h4', { text: 'Suggestions' }), list]));
    }
    if (result.strengths && result.strengths.length) lists.append(node('div', {}, [node('h4', { text: 'Strengths' }), node('ul', {}, result.strengths.map((item) => node('li', { text: item })))]));
    if (result.risks && result.risks.length) lists.append(node('div', {}, [node('h4', { text: 'Review carefully' }), node('ul', {}, result.risks.map((item) => node('li', { text: item })))]));
    if (lists.children.length) container.append(lists);
    if (result.privacy_note) container.append(node('p', { className: 'fine-print', text: result.privacy_note }));
  }

  async function uploadFile(path, file, extra = {}) {
    const form = new FormData();
    form.append('file', file);
    Object.entries(extra).forEach(([key, value]) => form.append(key, value));
    return api(path, { method: 'POST', body: form });
  }

  async function initProfile() {
    $('#addExperience').addEventListener('click', () => addEntry('experienceTemplate', 'experienceList'));
    $('#addEducation').addEventListener('click', () => addEntry('educationTemplate', 'educationList'));
    $('#addProject').addEventListener('click', () => addEntry('projectTemplate', 'projectList'));
    try {
      const data = await api('/api/profile');
      applyProfileDraft(data.profile || {}, data.completion || {});
    } catch (error) { toast(error.message, 'error'); }

    $('#loadStarterButton').addEventListener('click', async () => {
      try {
        const data = await api('/api/profile/starter');
        applyProfileDraft(data.profile || {}, data.completion || {}, data.message);
        $('#profileSaveStatus').textContent = 'QA starter loaded as a draft. Replace its placeholders, then save your facts.';
        toast('Editable QA starter loaded.');
      } catch (error) { toast(error.message, 'error'); }
    });
    $('#importProfileButton').addEventListener('click', () => $('#profileJsonInput').click());
    $('#importResumeButton').addEventListener('click', () => $('#resumeImportInput').click());
    $('#profileJsonInput').addEventListener('change', async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        const data = await uploadFile('/api/profile/import', file);
        applyProfileDraft(data.profile || {}, data.completion || {}, data.message);
        $('#profileSaveStatus').textContent = 'Imported profile is a draft. Review it and choose Save master profile.';
        toast('Profile JSON imported as a reviewable draft.');
      } catch (error) { toast(error.message, 'error'); }
      finally { event.target.value = ''; }
    });
    $('#resumeImportInput').addEventListener('change', async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        const data = await uploadFile('/api/profile/import-document', file);
        applyProfileDraft(data.profile || {}, data.completion || {}, (data.notes || []).join(' '));
        $('#profileSaveStatus').textContent = 'Resume import is a draft. Check every fact, then save it.';
        toast(`Imported readable details from ${data.filename || 'your resume'}.`);
      } catch (error) { toast(error.message, 'error'); }
      finally { event.target.value = ''; }
    });
    $('#proofreadSummaryButton').addEventListener('click', async () => {
      const text = $('#summary').value.trim();
      if (!text) { toast('Add a summary before proofreading it.', 'warning'); return; }
      const button = $('#proofreadSummaryButton');
      button.disabled = true; button.textContent = 'Reviewing...';
      try {
        const result = await api('/api/ai/review', { method: 'POST', body: { task: 'proofread', text } });
        renderAiResult($('#profileAiResult'), result, (revised) => { $('#summary').value = revised; toast('Suggested summary revision placed in the editor. Review before saving.'); });
      } catch (error) { toast(error.message, 'error'); }
      finally { button.disabled = false; button.textContent = 'Proofread summary'; }
    });
    $('#profileForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!event.currentTarget.reportValidity()) return;
      const button = $('button[type="submit"]', event.currentTarget);
      button.disabled = true;
      $('#profileSaveStatus').textContent = 'Saving your master profile...';
      try {
        const data = await api('/api/profile', { method: 'PUT', body: profileFromForm() });
        profileTemplateActive = Boolean(data.profile && data.profile.is_starter_template);
        showProfileCompletion(data.completion || {});
        updateStarterNotice(data.profile || {});
        $('#profileSaveStatus').textContent = profileTemplateActive ? 'Saved as a starter draft. Replace all visible placeholders before exporting.' : 'Saved locally. Job-specific resumes will use these facts.';
        toast('Master profile saved.');
      } catch (error) {
        $('#profileSaveStatus').textContent = error.message;
        toast(error.message, 'error');
      } finally { button.disabled = false; }
    });
  }

  function addChipList(container, labels, className, emptyText) {
    clear(container);
    if (!labels || !labels.length) { container.append(node('p', { className: 'muted inline-note', text: emptyText })); return; }
    labels.forEach((label) => container.append(createChip(label, className)));
  }

  function renderResumePreview(resume) {
    const preview = $('#resumePreview');
    clear(preview);
    const header = node('header', { className: 'preview-header' });
    header.append(node('h2', { text: resume.full_name || 'Your name' }));
    if (resume.headline || resume.target_title) header.append(node('p', { text: resume.headline || resume.target_title }));
    const contact = [resume.location, resume.phone, resume.email, resume.linkedin_url, resume.portfolio_url].filter(Boolean).join(' | ');
    if (contact) header.append(node('small', { text: contact }));
    preview.append(header);
    const addSection = (title, content) => {
      if (!content) return;
      const section = node('section', { className: 'preview-section' }, [node('h3', { text: title })]);
      if (typeof content === 'string') section.append(node('p', { text: content })); else section.append(content);
      preview.append(section);
    };
    addSection('Professional Summary', resume.summary);
    addSection('Core Skills', (resume.skills || []).join(' / '));
    if (resume.experience && resume.experience.length) {
      const experiences = node('div');
      resume.experience.forEach((item) => {
        const itemEl = node('div', { className: 'preview-entry' });
        itemEl.append(node('strong', { text: [item.title, item.company].filter(Boolean).join(' | ') }));
        itemEl.append(node('span', { text: [item.location, item.start_date, item.current ? 'Present' : item.end_date].filter(Boolean).join(' | ') }));
        const list = node('ul');
        (item.bullets || []).slice(0, 4).forEach((bullet) => list.append(node('li', { text: bullet })));
        if (list.children.length) itemEl.append(list);
        experiences.append(itemEl);
      });
      addSection('Professional Experience', experiences);
    }
    if (resume.education && resume.education.length) {
      const list = node('div');
      resume.education.forEach((item) => list.append(node('p', { text: [item.degree, item.school, item.graduation].filter(Boolean).join(' | ') })));
      addSection('Education', list);
    }
    if (resume.projects && resume.projects.length) {
      const projects = node('div');
      resume.projects.forEach((item) => {
        const project = node('div', { className: 'preview-entry' });
        project.append(node('strong', { text: item.name || 'Project' }));
        if (item.description) project.append(node('p', { text: item.description }));
        const list = node('ul');
        (item.bullets || []).slice(0, 4).forEach((bullet) => list.append(node('li', { text: bullet })));
        if (list.children.length) project.append(list);
        projects.append(project);
      });
      addSection('Selected Projects', projects);
    }
    if (resume.certifications && resume.certifications.length) addSection('Certifications', resume.certifications.join(' | '));
  }

  function renderAnalysis(data) {
    builderAnalysis = data.analysis || {};
    builderResume = data.tailored_resume || {};
    $('#analysisWrap').hidden = false;
    $('#jobMatchScore').textContent = `${builderAnalysis.job_match_score || 0}%`;
    $('#readabilityScore').textContent = `${builderAnalysis.readability_score || 0}%`;
    $('#matchedCount').textContent = `${(builderAnalysis.matched_skills || []).length} matched`;
    $('#missingCount').textContent = `${(builderAnalysis.missing_skills || []).length} gaps`;
    $('#analysisDisclaimer').textContent = builderAnalysis.disclaimer || '';
    addChipList($('#matchedSkills'), builderAnalysis.matched_skills, 'chip-positive', 'No verified matching QA skills were found in the current profile.');
    addChipList($('#missingSkills'), builderAnalysis.missing_skills, 'chip-warning', 'No common QA gaps were detected. Review the full job description before applying.');
    const evidence = $('#evidenceList');
    clear(evidence);
    (builderAnalysis.evidence || []).slice(0, 6).forEach((item) => evidence.append(node('p', { text: `${item.skill}: ${(item.evidence || []).join(', ')}` })));
    const guidance = $('#guidanceList');
    clear(guidance);
    (builderAnalysis.guidance || []).forEach((item) => guidance.append(node('li', { text: item })));
    renderResumePreview(builderResume);
    $('#saveJobButton').disabled = false;
    $('#analysisWrap').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function builderValues() {
    return {
      title: $('#builderTitle').value.trim(),
      company: $('#builderCompany').value.trim(),
      source_url: $('#builderUrl').value.trim(),
      layout: $('#builderTemplate').value,
      description: $('#builderDescription').value.trim(),
    };
  }

  async function downloadResume() {
    const values = builderValues();
    const button = $('#generateResumeButton');
    button.disabled = true; button.textContent = 'Creating Word file...';
    try {
      const body = builderJobId ? { ...values, job_id: builderJobId } : values;
      const response = await fetch('/api/resumes/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || 'Could not create the Word file.');
      }
      const blob = await response.blob();
      const disposition = response.headers.get('content-disposition') || '';
      const nameMatch = disposition.match(/filename="?([^";]+)"?/i);
      const filename = nameMatch ? nameMatch[1] : 'Tailored_Resume.docx';
      const url = URL.createObjectURL(blob);
      const anchor = node('a', { href: url, download: filename });
      document.body.append(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
      toast('Your ATS-safe Word resume has been downloaded.');
      loadResumeVersions();
    } catch (error) { toast(error.message, 'error'); }
    finally { button.disabled = false; button.textContent = 'Download .docx'; }
  }

  async function loadResumeVersions() {
    const container = $('#resumeVersions');
    if (!container) return;
    try {
      const data = await api('/api/resumes');
      clear(container);
      if (!data.resumes.length) { container.append(node('p', { className: 'muted', text: 'Your exported Word versions will appear here.' })); return; }
      data.resumes.forEach((resume) => {
        const row = node('div', { className: 'version-row' });
        row.append(node('div', {}, [node('strong', { text: resume.title }), node('p', { text: `${resume.company || 'General version'} / ${formatDate(resume.created_at)}` })]));
        row.append(node('a', { className: 'button button-small button-secondary', href: `/api/resumes/${resume.id}/download`, text: 'Download .docx' }));
        container.append(row);
      });
    } catch (error) { container.textContent = error.message; }
  }

  async function initBuilder() {
    const draft = storageJson('careercraft_builder_draft', null);
    if (draft) {
      $('#builderTitle').value = draft.title || '';
      $('#builderCompany').value = draft.company || '';
      $('#builderUrl').value = draft.source_url || '';
      $('#builderDescription').value = draft.description || '';
      builderJobId = draft.id || null;
      localStorage.removeItem('careercraft_builder_draft');
      toast('Job details loaded from your inbox. Analyse them before exporting.');
    }
    $('#analyzeForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget);
      button.disabled = true; button.textContent = 'Analysing...';
      try { renderAnalysis(await api('/api/analyze', { method: 'POST', body: builderValues() })); }
      catch (error) { toast(error.message, 'error'); }
      finally { button.disabled = false; button.textContent = 'Analyse requirements ->'; }
    });
    $$('.builder-template-choice').forEach((choice) => choice.addEventListener('click', () => {
      const layout = choice.dataset.resumeLayout;
      if (!layout) return;
      $('#builderTemplate').value = layout;
      $$('.builder-template-choice').forEach((item) => item.classList.toggle('selected', item === choice));
    }));
    $('#builderTemplate').addEventListener('change', () => {
      const selected = $('#builderTemplate').value;
      $$('.builder-template-choice').forEach((item) => item.classList.toggle('selected', item.dataset.resumeLayout === selected));
    });
    $$('#analyzeForm input, #analyzeForm textarea').forEach((field) => field.addEventListener('input', () => {
      if (builderJobId) { $('#saveJobButton').disabled = false; $('#saveJobButton').textContent = 'Update saved job'; }
    }));
    $('#saveJobButton').addEventListener('click', async () => {
      try {
        const values = builderValues();
        const result = builderJobId
          ? await api(`/api/jobs/${builderJobId}`, { method: 'PATCH', body: values })
          : await api('/api/jobs', { method: 'POST', body: values });
        builderJobId = result.job.id;
        $('#saveJobButton').textContent = result.created === false ? 'Already in jobs inbox' : 'Saved to jobs inbox';
        $('#saveJobButton').disabled = true;
        toast(result.created === false ? 'That role is already saved.' : 'Role saved to your approval inbox.');
      } catch (error) { toast(error.message, 'error'); }
    });
    $('#aiResumeReviewButton').addEventListener('click', async () => {
      if (!builderResume || !builderAnalysis) { toast('Analyse the job before running a resume review.', 'warning'); return; }
      const button = $('#aiResumeReviewButton');
      button.disabled = true; button.textContent = 'Scanning...';
      try {
        const result = await api('/api/ai/review', { method: 'POST', body: { task: 'resume_review', resume: builderResume, analysis: builderAnalysis } });
        renderAiResult($('#builderAiResult'), result);
      } catch (error) { toast(error.message, 'error'); }
      finally { button.disabled = false; button.textContent = 'Run local AI scan'; }
    });
    $('#generateResumeButton').addEventListener('click', downloadResume);
    loadResumeVersions();
  }

  function currentJobFilters(status) {
    const query = new URLSearchParams({ status });
    const roleTrack = $('#roleTrackFilter');
    if (roleTrack && roleTrack.value && roleTrack.value !== 'All QA tracks') query.set('role_track', roleTrack.value);
    const productOnly = $('#productOnly');
    const salaryOnly = $('#salaryOnly');
    if (productOnly && productOnly.checked) query.set('product_only', 'true');
    if (salaryOnly && salaryOnly.checked) query.set('salary_only', 'true');
    return query;
  }

  async function loadJobs(status = 'new') {
    window.currentJobFilter = status;
    const container = $('#jobsList');
    if (!container) return;
    try {
      const data = await api(`/api/jobs?${currentJobFilters(status).toString()}`);
      clear(container);
      if (!data.jobs.length) {
        const title = status === 'new' ? 'No new roles yet' : status === 'closed' ? 'No closed roles' : 'No roles in this view';
        const copy = status === 'new' ? 'Refresh the multi-source QA feed, import a role, or paste a description into Tailor Resume.' : 'Change the filter or add a new role.';
        container.append(emptyState(title, copy, status === 'new' ? 'Tailor a pasted job' : '', status === 'new' ? '/builder' : ''));
      } else data.jobs.forEach((job) => container.append(createJobCard(job)));
      trackNewJobs(data.jobs);
      return data.jobs.length;
    } catch (error) { clear(container); container.append(emptyState('Jobs inbox unavailable', error.message)); return 0; }
  }

  function discoveryPayload() {
    return {
      query: ($('#jobQuery').value || 'qa test engineer').trim(),
      market: ($('#marketFilter').value || 'India').trim(),
      role_track: $('#roleTrackFilter').value,
      include_product_boards: true,
      product_only: $('#productOnly').checked,
      salary_only: $('#salaryOnly').checked,
    };
  }

  function renderSourceReport(report, heading) {
    const container = $('#sourceReport');
    if (!container) return;
    clear(container);
    container.hidden = false;
    if (heading) container.append(node('p', { className: 'source-report-title', text: heading }));
    const list = node('div', { className: 'source-report-list' });
    (report || []).forEach((item) => {
      const status = item.status || 'reference';
      const detail = item.detail || item.type || '';
      list.append(node('p', { className: `source-status ${status}` }, [
        node('strong', { text: item.source || item.name || 'Source' }),
        node('span', { text: `${status}${typeof item.count === 'number' ? `: ${item.count} QA roles` : ''}${detail ? ` - ${detail}` : ''}` }),
      ]));
    });
    if (list.children.length) container.append(list);
  }

  function selectJobFilter(status) {
    const tab = $(`.filter-tab[data-job-filter="${status}"]`);
    if (!tab) return;
    $$('.filter-tab').forEach((item) => item.classList.toggle('active', item === tab));
    loadJobs(status);
  }

  function renderLatestSearchResults(jobs, market) {
    const panel = $('#searchResultsPanel');
    const list = $('#latestSearchList');
    const count = $('#latestSearchCount');
    const status = $('#latestSearchStatus');
    if (!panel || !list || !count || !status) return;
    clear(list);
    panel.hidden = false;
    count.textContent = `${jobs.length} ${jobs.length === 1 ? 'role' : 'roles'}`;
    status.textContent = jobs.length
      ? `These are the exact records from the latest ${market || 'selected-market'} search. They remain visible here even when a record is already approved, rejected, or filtered out of the inbox.`
      : 'No eligible roles were returned by this search. Review the source report, change the query, or add a role manually.';
    jobs.slice(0, 30).forEach((job) => {
      const card = node('article', { className: 'latest-search-card' });
      card.append(node('h3', { text: job.title || 'Untitled role' }));
      card.append(node('p', { className: 'job-meta', text: [job.company, job.location, job.source].filter(Boolean).join(' / ') }));
      card.append(node('p', { className: 'job-insight', text: (job.description || '').slice(0, 260) }));
      const tags = node('div', { className: 'job-tags' }, [
        createChip(job.search_result_state === 'created' ? 'New from this search' : 'Already saved', job.search_result_state === 'created' ? 'chip-positive' : 'chip-neutral'),
        createChip(titleCase(job.status || 'new'), `status-${job.status || 'new'}`),
        createChip(job.role_track || 'Test Engineer', 'chip-neutral'),
      ]);
      card.append(tags);
      const actions = node('div', { className: 'job-actions' });
      if (job.source_url) actions.append(node('a', { className: 'text-button', href: job.source_url, target: '_blank', rel: 'noopener noreferrer', text: 'Open original listing' }));
      const tailor = node('button', { className: 'button button-small button-primary', type: 'button', text: 'Tailor resume' });
      tailor.addEventListener('click', () => { storeBuilderDraft(job); window.location.href = '/builder'; });
      const view = node('button', { className: 'text-button', type: 'button', text: `View ${titleCase(job.status || 'new')} queue` });
      view.addEventListener('click', () => selectJobFilter(job.status === 'closed' ? 'closed' : job.status === 'new' ? 'new' : 'all'));
      actions.append(tailor, view);
      card.append(actions);
      list.append(card);
    });
  }

  async function refreshWatchedJobs({ silent = false, forceRefresh = false } = {}) {
    const summary = $('#queueRefreshSummary');
    if (summary && !silent) {
      summary.hidden = false;
      summary.textContent = 'Searching public job sources... results will appear below when each source check completes.';
    }
    try {
      const data = await api('/api/jobs/discover', { method: 'POST', body: { ...discoveryPayload(), force_refresh: forceRefresh } });
      if (!silent) toast(data.message);
      const cachedLabel = data.cached ? 'Saved search' : 'Live refresh';
      renderSourceReport(data.source_report, `${cachedLabel}: ${formatDate(data.checked_at)}`);
      renderLatestSearchResults(data.results || data.jobs || [], discoveryPayload().market);
      if (data.added > 0) selectJobFilter('new');
      const activeStatus = window.currentJobFilter || 'new';
      const visibleCount = await loadJobs(activeStatus);
      if (summary) {
        summary.hidden = false;
        summary.textContent = visibleCount
          ? `${data.reviewed || 0} roles were recorded in Latest search results. ${data.added || 0} new role(s) are in New; ${visibleCount} role(s) are visible in the current ${activeStatus} inbox view.`
          : `${data.reviewed || 0} roles were recorded in Latest search results. The current ${activeStatus} inbox filter has no visible roles; the exact search cards remain above.`;
      }
      return data;
    } catch (error) {
      if (!silent) toast(error.message, 'error');
      return null;
    }
  }

  function configureJobWatch() {
    const control = $('#watchJobs');
    if (!control) return;
    const stored = storageJson(JOB_WATCH_KEY, null);
    if (stored && typeof stored === 'object') {
      control.checked = Boolean(stored.enabled);
      if (stored.query) $('#jobQuery').value = stored.query;
      if (stored.market) $('#marketFilter').value = stored.market;
      if (stored.role_track) $('#roleTrackFilter').value = stored.role_track;
      $('#productOnly').checked = Boolean(stored.product_only);
      $('#salaryOnly').checked = Boolean(stored.salary_only);
    }
    const start = () => {
      if (jobWatchTimer) window.clearInterval(jobWatchTimer);
      const config = { ...discoveryPayload(), enabled: control.checked };
      localStorage.setItem(JOB_WATCH_KEY, JSON.stringify(config));
      if (!control.checked) return;
      jobWatchTimer = window.setInterval(() => refreshWatchedJobs({ silent: true }), JOB_WATCH_INTERVAL_MS);
    };
    control.addEventListener('change', () => {
      start();
      if (control.checked) toast('CareerCraft will refresh public QA sources every six hours while this page stays open.');
      else toast('Automatic in-browser checks are off.');
    });
    ['#jobQuery', '#marketFilter', '#roleTrackFilter', '#productOnly', '#salaryOnly'].forEach((selector) => $(selector).addEventListener('change', () => {
      start(); loadJobs(window.currentJobFilter || 'new');
    }));
    start();
  }

  function updateLinkedInLink() {
    const query = $('#linkedinQuery').value.trim() || 'QA Engineer';
    $('#linkedinAlertLink').href = `https://www.linkedin.com/jobs/search/?keywords=${encodeURIComponent(query)}`;
  }

  function updateGoogleSearchLink() {
    const link = $('#googleSearchLink');
    const queryInput = $('#jobQuery');
    const market = $('#marketFilter');
    if (!link || !queryInput || !market) return;
    const roleTrack = $('#roleTrackFilter');
    const track = roleTrack && roleTrack.value !== 'All QA tracks' ? roleTrack.value : '';
    const query = `${queryInput.value.trim() || 'qa test engineer'} ${track} jobs ${market.value || 'India'} (site:greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com OR site:myworkdayjobs.com)`;
    link.href = `https://www.google.com/search?q=${encodeURIComponent(query)}`;
  }

  async function showSourceCatalogue() {
    try {
      const data = await api('/api/job-sources');
      renderSourceReport((data.sources || []).map((item) => ({ ...item, status: 'reference' })), data.notice || 'Public sources used for the next refresh:');
    } catch (error) { toast(error.message, 'error'); }
  }

  function initJobs() {
    $('#discoverJobsForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget);
      button.disabled = true; button.textContent = 'Refreshing...';
      try { await refreshWatchedJobs({ forceRefresh: true }); }
      finally { button.disabled = false; button.textContent = 'Refresh QA roles'; }
    });
    $('#showSourcesButton').addEventListener('click', showSourceCatalogue);
    $('#linkedinQuery').addEventListener('input', updateLinkedInLink);
    $('#jobQuery').addEventListener('input', updateGoogleSearchLink);
    $('#marketFilter').addEventListener('change', updateGoogleSearchLink);
    $('#roleTrackFilter').addEventListener('change', updateGoogleSearchLink);
    $('#enableJobsNotifications').addEventListener('click', notificationPermission);
    const latestQueueButton = $('#latestSearchQueueButton');
    if (latestQueueButton) latestQueueButton.addEventListener('click', () => selectJobFilter('new'));
    $('#linkedinManualForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!event.currentTarget.reportValidity()) return;
      const button = $('button[type="submit"]', event.currentTarget);
      button.disabled = true; button.textContent = 'Adding...';
      try {
        const result = await api('/api/jobs', { method: 'POST', body: {
          title: $('#linkedinTitle').value.trim(),
          company: $('#linkedinCompany').value.trim(),
          source_url: $('#linkedinUrl').value.trim(),
          description: $('#linkedinDescription').value.trim(),
          source: 'LinkedIn (manual)',
          source_note: 'Manually added from LinkedIn by the user. CareerCraft did not scrape the listing.',
        } });
        toast(result.created ? 'LinkedIn role added to your approval inbox.' : 'That role is already in your inbox.');
        event.currentTarget.reset();
        await loadJobs('new');
      } catch (error) { toast(error.message, 'error'); }
      finally { button.disabled = false; button.textContent = 'Add LinkedIn role to inbox'; }
    });
    $('#importJobsButton').addEventListener('click', () => $('#jobsImportInput').click());
    $('#jobsImportInput').addEventListener('change', async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        const result = await uploadFile('/api/jobs/import', file, { source: 'LinkedIn / user import' });
        toast(result.message);
        await loadJobs('new');
      } catch (error) { toast(error.message, 'error'); }
      finally { event.target.value = ''; }
    });
    $$('.filter-tab').forEach((button) => button.addEventListener('click', () => {
      $$('.filter-tab').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      loadJobs(button.dataset.jobFilter);
    }));
    configureJobWatch();
    updateGoogleSearchLink();
    updateLinkedInLink();
    loadJobs('new');
  }

  function applicationContactValues(prefix = '') {
    const value = (id) => {
      const field = $(`#${prefix}${id}`);
      return field ? field.value.trim() : '';
    };
    return {
      contact_name: value('ContactName'),
      contact_role: value('ContactRole'),
      contact_email: value('ContactEmail'),
      contact_phone: value('ContactPhone'),
      referral_name: value('ReferralName'),
      referral_contact: value('ReferralContact'),
      next_step: value('NextStep'),
    };
  }

  async function updateApplication(id, status, notes, contact, button) {
    button.disabled = true;
    try {
      await api(`/api/applications/${id}`, { method: 'PATCH', body: { status, notes, ...contact } });
      toast('Application updated.');
      loadApplications();
    } catch (error) { toast(error.message, 'error'); }
    finally { button.disabled = false; }
  }

  async function loadApplications() {
    const container = $('#applicationsList');
    if (!container) return;
    try {
      const data = await api('/api/applications');
      clear(container);
      if (!data.applications.length) {
        container.append(emptyState('No approved roles yet', 'Approve a role from the Jobs inbox when you want to track it here.', 'Review jobs', '/jobs'));
        return;
      }
      data.applications.forEach((application) => {
        const row = node('article', { className: 'application-row' });
        const text = node('div', { className: 'application-main' });
        text.append(node('h3', { text: application.title }));
        text.append(node('p', { text: [application.company, application.location, `Saved ${formatDate(application.created_at)}`].filter(Boolean).join(' / ') }));
        if (application.source_url) text.append(node('a', { className: 'text-link', href: application.source_url, target: '_blank', rel: 'noopener noreferrer', text: 'Open application page' }));
        const control = node('div', { className: 'application-controls' });
        const select = node('select', { 'aria-label': `Status for ${application.title}` });
        ['approved', 'applied', 'interview', 'offer'].forEach((status) => {
          const option = node('option', { value: status, text: titleCase(status), selected: application.status === status });
          select.append(option);
        });
        const notes = node('textarea', { placeholder: 'Private notes', 'aria-label': `Private notes for ${application.title}`, value: application.notes || '' });
        const save = node('button', { className: 'button button-small button-secondary', type: 'button', text: 'Update' });
        const fieldPrefix = `application${application.id}`;
        const contactSummary = node('div', { className: 'application-contact-summary' });
        if (application.application_kind) contactSummary.append(createChip(application.application_kind, 'chip-neutral'));
        if (application.contact_name) contactSummary.append(createChip(`HR: ${application.contact_name}`, 'chip-positive'));
        if (application.referral_name) contactSummary.append(createChip(`Referral: ${application.referral_name}`, 'chip-positive'));
        if (application.next_step) contactSummary.append(createChip('Follow-up set', 'chip-warning'));
        if (contactSummary.children.length) text.append(contactSummary);
        const details = node('details', { className: 'application-details' });
        details.append(node('summary', { text: 'Recruiter, referral, and follow-up details' }));
        const detailGrid = node('div', { className: 'form-grid two-col compact-fields' });
        const detailFields = [
          ['ContactName', 'HR / recruiter name', application.contact_name || '', 'text'],
          ['ContactRole', 'Role / team', application.contact_role || '', 'text'],
          ['ContactEmail', 'Email', application.contact_email || '', 'email'],
          ['ContactPhone', 'Phone', application.contact_phone || '', 'tel'],
          ['ReferralName', 'Referral name', application.referral_name || '', 'text'],
          ['ReferralContact', 'Referral contact', application.referral_contact || '', 'text'],
        ];
        detailFields.forEach(([name, label, value, type]) => {
          const input = node('input', { id: `${fieldPrefix}${name}`, type, value, placeholder: label });
          detailGrid.append(node('label', { text: label }, [input]));
        });
        const nextStep = node('textarea', { id: `${fieldPrefix}NextStep`, rows: 2, value: application.next_step || '', placeholder: 'Next step / follow-up' });
        detailGrid.append(node('label', { className: 'span-two', text: 'Next step / follow-up' }, [nextStep]));
        details.append(detailGrid);
        save.addEventListener('click', () => updateApplication(application.id, select.value, notes.value, applicationContactValues(fieldPrefix), save));
        const close = node('button', { className: 'text-button danger', type: 'button', text: 'Close' });
        close.addEventListener('click', () => closeJob(application.job_id));
        control.append(select, notes, save, close);
        row.append(text, control, details);
        container.append(row);
      });
    } catch (error) { clear(container); container.append(emptyState('Application pipeline unavailable', error.message)); }
  }

  function manualApplicationValues() {
    return {
      title: $('#manualApplicationTitleInput').value.trim(),
      company: $('#manualApplicationCompany').value.trim(),
      application_kind: $('#manualApplicationKind').value,
      status: $('#manualApplicationStatus').value,
      location: $('#manualApplicationLocation').value.trim(),
      role_track: $('#manualApplicationTrack').value,
      source_url: $('#manualApplicationUrl').value.trim(),
      description: $('#manualApplicationDescription').value.trim(),
      notes: $('#manualApplicationNotes').value.trim(),
      ...applicationContactValues('manual'),
    };
  }

  function updateApplicationGoogleSearchLink() {
    const link = $('#applicationGoogleSearchLink');
    const query = $('#applicationGoogleQuery');
    const market = $('#applicationGoogleMarket');
    const track = $('#applicationGoogleTrack');
    if (!link || !query || !market || !track) return;
    const selectedTrack = track.value !== 'All QA tracks' ? track.value : '';
    const search = `${query.value.trim() || 'QA test engineer'} ${selectedTrack} jobs ${market.value.trim() || 'India'} (site:greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com OR site:myworkdayjobs.com)`;
    link.href = `https://www.google.com/search?q=${encodeURIComponent(search)}`;
  }

  function initApplications() {
    ['#applicationGoogleQuery', '#applicationGoogleMarket', '#applicationGoogleTrack'].forEach((selector) => {
      const field = $(selector);
      if (field) field.addEventListener('input', updateApplicationGoogleSearchLink);
      if (field) field.addEventListener('change', updateApplicationGoogleSearchLink);
    });
    updateApplicationGoogleSearchLink();
    const contactToggle = $('#toggleContactFields');
    const contactFields = $('#manualContactFields');
    if (contactToggle && contactFields) {
      contactToggle.addEventListener('click', () => {
        const opening = contactFields.hidden;
        contactFields.hidden = !opening;
        contactToggle.setAttribute('aria-expanded', String(opening));
        clear(contactToggle);
        contactToggle.append(node('span', { text: opening ? '-' : '+' }), document.createTextNode(opening ? ' Hide HR / recruiter or referral details' : ' Add HR / recruiter or referral details'));
      });
    }
    const form = $('#manualApplicationForm');
    if (form) {
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        const button = $('button[type="submit"]', form);
        button.disabled = true; button.textContent = 'Adding...';
        try {
          const result = await api('/api/applications', { method: 'POST', body: manualApplicationValues() });
          toast(result.message || 'Manual opportunity added.');
          form.reset();
          if (contactFields) contactFields.hidden = true;
          if (contactToggle) {
            contactToggle.setAttribute('aria-expanded', 'false');
            clear(contactToggle);
            contactToggle.append(node('span', { text: '+' }), document.createTextNode(' Add HR / recruiter or referral details'));
          }
          await loadApplications();
        } catch (error) { toast(error.message, 'error'); }
        finally { button.disabled = false; button.textContent = 'Add to pipeline'; }
      });
    }
    loadApplications();
  }

  async function loadAiStatus() {
    const status = $('#aiStatus');
    if (!status) return;
    try {
      const data = await api('/api/ai/status');
      const model = data.selected_model || 'qwen2.5:1.5b';
      status.textContent = !data.installed
        ? 'Ollama is not installed. CareerCraft is using its built-in local spelling and structure review until Ollama is installed.'
        : data.available
        ? (data.selected_installed ? `Ollama is ready with ${model}. Resume review stays on this device.` : `Ollama is running, but ${model} is not installed yet. The built-in review remains available.`)
        : 'Ollama is not running. CareerCraft will use its built-in local spelling and structure review until you enable it.';
      const command = $('#aiSetupCommand');
      if (command) { command.hidden = false; command.textContent = data.setup_command || `ollama pull ${model}`; }
      const start = $('#startAiService');
      const pull = $('#pullAiModel');
      if (start) start.disabled = !data.installed || data.available;
      if (pull) pull.disabled = !data.installed || data.selected_installed;
    } catch (error) { status.textContent = error.message; }
  }

  function initResources() {
    loadAiStatus();
    // The local model can finish downloading after this page has loaded.
    // Refresh the small localhost status check so users do not see a stale
    // "installing" message once Ollama is ready.
    window.setInterval(loadAiStatus, 10000);
    const refresh = $('#refreshAiStatus');
    if (refresh) refresh.addEventListener('click', loadAiStatus);
    const start = $('#startAiService');
    const pull = $('#pullAiModel');
    const runLocalAiAction = async (button, path, busyText) => {
      if (!button) return;
      const original = button.textContent;
      button.disabled = true;
      button.textContent = busyText;
      try {
        const result = await api(path, { method: 'POST', body: {} });
        toast(result.message || 'Local AI action started.');
      } catch (error) { toast(error.message, 'error'); }
      finally {
        button.textContent = original;
        window.setTimeout(loadAiStatus, 2500);
      }
    };
    if (start) start.addEventListener('click', () => runLocalAiAction(start, '/api/ai/start', 'Starting...'));
    if (pull) pull.addEventListener('click', () => runLocalAiAction(pull, '/api/ai/pull', 'Downloading...'));
  }

  function workspaceChangeCard(change, index) {
    const card = node('article', { className: 'workspace-change' });
    card.append(node('h3', { text: `${index + 1}. ${change.summary || change.path}` }));
    card.append(node('p', { className: 'workspace-path', text: change.path || '' }));
    const before = node('pre', { className: 'workspace-code', text: change.search || '' });
    const after = node('pre', { className: 'workspace-code workspace-code-after', text: change.replace || '' });
    const details = node('details', { className: 'workspace-change-details' }, [
      node('summary', { text: 'Show exact replacement' }),
      node('p', { className: 'workspace-code-label', text: 'Find exactly once' }), before,
      node('p', { className: 'workspace-code-label', text: 'Replace with' }), after,
    ]);
    card.append(details);
    return card;
  }

  function renderWorkspaceChatResult(result) {
    const container = $('#workspaceChatResult');
    if (!container) return;
    const bubble = node('div', { className: 'chat-bubble assistant-bubble' });
    bubble.append(node('p', { className: 'chat-sender', text: result.provider || 'CareerCraft local AI' }));
    bubble.append(node('p', { className: 'workspace-answer', text: result.answer || 'I could not prepare a response.' }));
    if (result.caution) bubble.append(node('p', { className: 'callout-warning', text: result.caution }));
    const changes = Array.isArray(result.proposed_changes) ? result.proposed_changes : [];
    if (changes.length) {
      bubble.append(node('h3', { className: 'workspace-changes-heading', text: 'Proposed source changes' }));
      const changesList = node('div', { className: 'workspace-changes' });
      changes.forEach((change, index) => changesList.append(workspaceChangeCard(change, index)));
      bubble.append(changesList);
      if (result.proposal_id) {
        const apply = node('button', { className: 'button button-primary', type: 'button', text: 'Apply reviewed proposal' });
        apply.addEventListener('click', async () => {
          if (!window.confirm('Apply these exact reviewed local source changes? CareerCraft will not restart automatically.')) return;
          apply.disabled = true; apply.textContent = 'Applying...';
          try {
            const response = await api('/api/workspace-chat/apply', { method: 'POST', body: { proposal_id: result.proposal_id, confirm: true } });
            toast(response.message || 'Local proposal applied. Restart CareerCraft to load it.');
            apply.textContent = 'Applied - restart CareerCraft';
          } catch (error) {
            toast(error.message, 'error');
            apply.disabled = false; apply.textContent = 'Apply reviewed proposal';
          }
        });
        bubble.append(node('div', { className: 'tool-actions workspace-apply' }, [apply]));
      }
    } else {
      bubble.append(node('p', { className: 'chat-meta', text: 'No source changes proposed.' }));
    }
    container.append(bubble);
    container.scrollTop = container.scrollHeight;
  }

  function renderWorkspaceChatProgress(detail, stage) {
    const container = $('#workspaceChatResult');
    if (!container) return null;
    let progress = $('.chat-progress', container);
    if (!progress) {
      progress = node('div', { className: 'chat-bubble assistant-bubble chat-progress' });
      progress.append(node('p', { className: 'chat-sender', text: 'CareerCraft local AI' }));
      progress.append(node('p', { className: 'workspace-answer' }));
      container.append(progress);
    }
    $('.workspace-answer', progress).textContent = `${stage === 'searching' ? 'Searching public job sources' : 'Thinking locally'}... ${detail || ''}`;
    container.scrollTop = container.scrollHeight;
    return progress;
  }

  async function pollWorkspaceChat(taskId) {
    const startedAt = Date.now();
    for (;;) {
      const task = await api(`/api/workspace-chat/status/${encodeURIComponent(taskId)}`);
      renderWorkspaceChatProgress(task.detail, task.stage);
      if (task.state === 'complete') return task.result;
      if (task.state === 'error') throw new Error(task.detail || 'The local assistant could not complete the request.');
      if (Date.now() - startedAt > 120000) throw new Error('The local assistant took too long to respond. Check Ollama status and try again.');
      await new Promise((resolve) => window.setTimeout(resolve, 700));
    }
  }

  async function loadAssistantAiStatus() {
    const status = $('#assistantAiStatus');
    if (!status) return;
    try {
      const data = await api('/api/ai/status');
      const model = data.selected_model || 'qwen2.5:1.5b';
      status.textContent = data.available && data.selected_installed
        ? `Ready: Ollama is using ${model} locally.`
        : !data.installed
        ? `Ollama is not installed. Install it, then run: ${data.setup_command || `ollama pull ${model}`}.`
        : `Ollama needs ${model}. Start it and download the model from Guides & checks.`;
    } catch (error) { status.textContent = error.message; }
  }

  function initAssistant() {
    loadAssistantAiStatus();
    const form = $('#workspaceChatForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const input = $('#workspaceChatInput');
      const button = $('#workspaceChatSubmit');
      if (!input.value.trim()) { toast('Write a source-change request first.', 'warning'); return; }
      const message = input.value.trim();
      const thread = $('#workspaceChatResult');
      thread.append(node('div', { className: 'chat-bubble user-bubble' }, [node('p', { text: message })]));
      thread.scrollTop = thread.scrollHeight;
      input.value = '';
      input.style.height = '';
      button.disabled = true; button.textContent = 'Thinking locally...';
      try {
        const started = await api('/api/workspace-chat/start', { method: 'POST', body: { message } });
        renderWorkspaceChatProgress('Request queued locally.', 'queued');
        const result = await pollWorkspaceChat(started.task_id);
        const progress = $('.chat-progress', thread);
        if (progress) progress.remove();
        renderWorkspaceChatResult(result);
      } catch (error) { toast(error.message, 'error'); }
      finally { button.disabled = false; button.textContent = 'Send'; }
    });
    const input = $('#workspaceChatInput');
    input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 130)}px`; });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
    });
  }

  function afterAuth(form, result) {
    updateCsrfToken(result.csrf_token);
    const next = (form && form.dataset.next) || '/';
    window.location.href = next.startsWith('/') && !next.startsWith('//') ? next : '/';
  }

  function initSignIn() {
    const form = $('#signInForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const button = $('button[type="submit"]', form);
      button.disabled = true;
      try {
        const result = await api('/api/auth/login', { method: 'POST', body: { email: $('#signInEmail').value.trim(), password: $('#signInPassword').value } });
        afterAuth(form, result);
      } catch (error) { toast(error.message, 'error'); }
      finally { button.disabled = false; }
    });
  }

  function initSignUp() {
    const form = $('#signUpForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const button = $('button[type="submit"]', form);
      button.disabled = true;
      try {
        const result = await api('/api/auth/register', { method: 'POST', body: { display_name: $('#signUpName').value.trim(), email: $('#signUpEmail').value.trim(), password: $('#signUpPassword').value } });
        afterAuth(form, result);
      } catch (error) { toast(error.message, 'error'); }
      finally { button.disabled = false; }
    });
  }

  function initAccount() {
    const form = $('#accountForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const current = $('#accountCurrentPassword').value;
      const next = $('#accountNewPassword').value;
      if (next && !current) { toast('Enter your current password to choose a new one.', 'warning'); return; }
      const button = $('button[type="submit"]', form);
      button.disabled = true;
      try {
        const result = await api('/api/account', { method: 'PATCH', body: { display_name: $('#accountDisplayName').value.trim(), current_password: current, new_password: next } });
        $('#accountCurrentPassword').value = '';
        $('#accountNewPassword').value = '';
        toast(result.message || 'Account settings saved.');
        if (result.user && result.user.display_name) window.setTimeout(() => window.location.reload(), 450);
      } catch (error) { toast(error.message, 'error'); }
      finally { button.disabled = false; }
    });
  }

  function initSessionActions() {
    const signOut = $('#signOutButton');
    if (!signOut) return;
    signOut.addEventListener('click', async () => {
      signOut.disabled = true;
      try { await api('/api/auth/logout', { method: 'POST', body: {} }); }
      catch (_) { /* The browser will still leave this private route. */ }
      finally { window.location.href = '/sign-in'; }
    });
  }

  function runStatusChip(status) {
    return createChip(titleCase(status || 'in_progress'), `status-${status || 'new'}`);
  }

  function renderLabRuns(container, runs, compact = false) {
    if (!container) return;
    clear(container);
    if (!runs.length) {
      container.append(emptyState('No QA evidence yet', 'Run a real scenario, then save the expected result, actual result, and any artifact reference.', 'Record a test run', '/lab/runs'));
      return;
    }
    runs.slice(0, compact ? 4 : 60).forEach((run) => {
      const item = node('article', { className: 'lab-run-item' });
      const head = node('div', { className: 'lab-run-item-head' }, [node('div', {}, [node('h3', { text: titleCase(String(run.scenario_slug || '').replace(/-/g, ' ')) }), node('p', { text: `${titleCase(run.suite)} / ${formatDate(run.created_at)}` })]), runStatusChip(run.status)]);
      item.append(head);
      if (run.notes) item.append(node('p', { className: 'lab-run-notes', text: run.notes }));
      container.append(item);
    });
  }

  async function loadLabSummary() {
    const data = await api('/api/lab/quality-summary');
    const scenario = $('#labScenarioCount');
    const catalog = $('#labCatalogCount');
    const passed = $('#labPassedRuns');
    const gates = $('#labGateCount');
    if (scenario) scenario.textContent = data.scenarios || 0;
    if (catalog) catalog.textContent = data.catalog_items || 0;
    if (passed) passed.textContent = (data.runs && data.runs.passed) || 0;
    if (gates) gates.textContent = (data.gates || []).length;
    const gateList = $('#labGates');
    if (gateList) { clear(gateList); (data.gates || []).forEach((gate) => gateList.append(node('li', { text: gate }))); }
    return data;
  }

  async function loadLabRuns(container = $('#labRunsList'), compact = false) {
    const data = await api('/api/lab/runs');
    renderLabRuns(container, data.runs || [], compact);
    return data.runs || [];
  }

  function initLabOverview() {
    Promise.all([loadLabSummary(), loadLabRuns($('#labRecentRuns'), true)]).catch((error) => toast(error.message, 'error'));
  }

  async function loadLabCatalog() {
    const container = $('#labCatalogList');
    if (!container) return [];
    try {
      const data = await api('/api/lab/catalog');
      clear(container);
      if (!data.items.length) container.append(emptyState('No catalog data', 'Refresh the QA Lab to seed synthetic practice data.'));
      data.items.forEach((item) => {
        const row = node('div', { className: 'lab-catalog-item' }, [node('div', {}, [node('strong', { text: item.name }), node('span', { text: `${item.sku} / ${item.category}` })]), node('div', { className: 'lab-catalog-values' }, [node('span', { text: `Rs. ${Number(item.price).toFixed(2)}` }), node('span', { text: `${item.stock} in stock` })])]);
        container.append(row);
      });
      const product = $('#labOrderProduct');
      if (product && !product.value && data.items[0]) product.value = data.items[0].id;
      return data.items;
    } catch (error) { clear(container); container.append(emptyState('Catalog unavailable', error.message)); return []; }
  }

  function initLabApi() {
    loadLabCatalog();
    const refresh = $('#refreshLabCatalog');
    if (refresh) refresh.addEventListener('click', () => loadLabCatalog());
    const form = $('#labOrderForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const button = $('button[type="submit"]', form);
      const response = $('#labOrderResponse');
      button.disabled = true;
      try {
        const result = await api('/api/lab/orders', { method: 'POST', body: { customer_name: $('#labOrderCustomer').value.trim(), idempotency_key: $('#labOrderIdempotency').value.trim(), items: [{ product_id: Number($('#labOrderProduct').value), quantity: Number($('#labOrderQuantity').value) }] } });
        response.textContent = JSON.stringify(result, null, 2);
        toast(result.message || 'Synthetic order created.');
        await loadLabCatalog();
      } catch (error) { response.textContent = JSON.stringify({ error: error.message }, null, 2); toast(error.message, 'error'); }
      finally { button.disabled = false; }
    });
  }

  function initLabRuns() {
    const form = $('#labRunForm');
    const scenarioFromQuery = new URLSearchParams(window.location.search).get('scenario');
    if (scenarioFromQuery && $('#labRunScenario')) $('#labRunScenario').value = scenarioFromQuery;
    loadLabRuns().catch((error) => toast(error.message, 'error'));
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', form);
      button.disabled = true;
      try {
        const result = await api('/api/lab/runs', { method: 'POST', body: { scenario_slug: $('#labRunScenario').value, suite: $('#labRunSuite').value, status: $('#labRunStatus').value, notes: $('#labRunNotes').value.trim() } });
        toast(result.message || 'QA run saved.');
        $('#labRunNotes').value = '';
        await loadLabRuns();
      } catch (error) { toast(error.message, 'error'); }
      finally { button.disabled = false; }
    });
  }

  function initNotifications() {
    const globalButton = $('#enableNotifications');
    if (globalButton) globalButton.addEventListener('click', notificationPermission);
  }

  document.addEventListener('DOMContentLoaded', () => {
    initNotifications();
    initSessionActions();
    if (page === 'sign-in') initSignIn();
    if (page === 'sign-up') initSignUp();
    if (page === 'account') initAccount();
    if (page === 'dashboard') loadDashboard();
    if (page === 'profile') initProfile();
    if (page === 'builder') initBuilder();
    if (page === 'resumes') loadResumeVersions();
    if (page === 'jobs') initJobs();
    if (page === 'applications') initApplications();
    if (page === 'resources') initResources();
    if (page === 'assistant') initAssistant();
    if (page === 'lab') initLabOverview();
    if (page === 'lab-api') initLabApi();
    if (page === 'lab-runs') initLabRuns();
  });
}());
