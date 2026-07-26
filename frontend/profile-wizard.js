/**
 * profile-wizard.js — shared step-by-step "Profile & Voice Guide" builder.
 * Used by profile.html (AI suggestions on) and register.html (AI suggestions
 * off — no resume is on the server yet during signup).
 *
 * Renders structured answers to/from the same markdown that's stored as
 * profile_text and fed directly into resume/cover-letter prompts, so the
 * backend contract doesn't change.
 *
 * Exposes window.ProfileWizard = { mount, fieldsToMarkdown, markdownToFields, defaultFields }
 */
(function () {
  'use strict';

  var STYLE_ID = 'profile-wizard-styles';
  var CSS = [
    '.pw-root{font-size:.875rem;color:var(--text)}',
    '.pw-stepper{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:1.25rem}',
    '.pw-step-dot{display:flex;align-items:center;gap:.4rem;padding:.3rem .6rem;border-radius:999px;',
    'border:1px solid var(--card-border);cursor:pointer;font-size:.75rem;color:var(--muted);transition:border-color .15s,color .15s}',
    '.pw-step-dot:hover{border-color:var(--border)}',
    '.pw-step-dot.pw-active{border-color:var(--border);color:var(--primary);font-weight:600}',
    '.pw-step-dot.pw-done{color:var(--text)}',
    '.pw-step-num{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;',
    'border-radius:50%;background:var(--fill);font-size:.65rem;font-weight:700;flex-shrink:0}',
    '.pw-step-dot.pw-active .pw-step-num{background:var(--border);color:#fff}',
    '.pw-hint{font-size:.8rem;color:var(--muted);margin:0 0 .875rem;line-height:1.5}',
    '.pw-sublabel{display:block;font-weight:600;font-size:.8rem;margin-bottom:.4rem}',
    '.pw-field{margin-bottom:.875rem}',
    '.pw-field label{display:block;font-weight:600;font-size:.8rem;margin-bottom:.3rem}',
    '.pw-input,.pw-textarea{width:100%;padding:.5rem .75rem;border:1px solid var(--input-border);',
    'border-radius:6px;font-size:.875rem;font-family:inherit;background:var(--input-bg);color:var(--text)}',
    '.pw-textarea{resize:vertical;min-height:90px;line-height:1.6}',
    '.pw-input:focus,.pw-textarea:focus{outline:none;border-color:var(--border);box-shadow:0 0 0 3px rgba(99,102,241,.15)}',
    '.pw-list{display:flex;flex-direction:column;gap:.5rem;margin-bottom:.5rem}',
    '.pw-list-row{display:flex;gap:.5rem;align-items:center}',
    '.pw-list-row .pw-input{flex:1}',
    '.pw-btn-icon{flex-shrink:0;width:28px;height:28px;border:1px solid var(--input-border);border-radius:6px;',
    'background:var(--bg-card);color:var(--muted);cursor:pointer;font-size:.75rem;line-height:1}',
    '.pw-btn-icon:hover{border-color:var(--error);color:var(--error)}',
    '.pw-cards{display:flex;flex-direction:column;gap:.75rem;margin-bottom:.5rem}',
    '.pw-card{border:1px solid var(--card-border);border-radius:8px;padding:.75rem;background:var(--bg-page)}',
    '.pw-card-head{display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem}',
    '.pw-card-head .pw-input{flex:1;font-weight:600}',
    '.pw-ai-row{display:flex;align-items:center;gap:.625rem;margin-bottom:.75rem;flex-wrap:wrap}',
    '.pw-ai-status{font-size:.775rem;color:var(--muted)}',
    '.pw-ai-status.pw-ai-error{color:var(--error)}',
    '.pw-preview{font-size:.825rem;line-height:1.7;padding:1rem 1.25rem;background:var(--bg-page);',
    'border:1px solid var(--card-border);border-radius:6px;white-space:pre-wrap}',
    '.pw-preview h1,.pw-preview h2,.pw-preview h3{color:var(--primary);font-weight:700;margin:1rem 0 .5rem;font-size:.9rem}',
    '.pw-preview ul,.pw-preview ol{margin:0 0 .75rem;padding-left:1.4rem}',
    '.pw-preview p{margin:0 0 .75rem;white-space:normal}',
    '.pw-nav{display:flex;justify-content:space-between;align-items:center;margin-top:1.25rem;gap:.75rem}',
    '.pw-btn-row{display:flex;gap:.5rem}',
    '.pw-btn{display:inline-flex;align-items:center;gap:.4rem;padding:.5rem 1.1rem;background:var(--primary);',
    'color:#fff;border:none;border-radius:6px;font-size:.825rem;font-weight:600;cursor:pointer}',
    '.pw-btn:hover{filter:brightness(1.12)}',
    '.pw-btn:disabled{opacity:.6;cursor:not-allowed}',
    '.pw-btn-ghost{background:var(--bg-card);color:var(--primary);border:1px solid var(--border)}',
    '.pw-btn-ghost:hover{background:var(--fill);filter:none}',
    '.pw-btn-sm{padding:.35rem .75rem;font-size:.775rem;align-self:flex-start}',
  ].join('');

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  var DEFAULT_DO = [
    'Write in first person, direct and conversational',
    'Lead with specifics: numbers, system names, outcomes, timelines',
    'Use active verbs: built, shipped, owned, designed, led, delivered',
    'Echo language from the job posting where it rings true',
  ];
  var DEFAULT_DONT = [
    '"I am excited to apply for..."',
    '"passion for", "leverage", "synergy", "results-driven"',
    'Vague claims without specifics ("improved processes significantly")',
    'Starting bullets with "Responsible for..."',
  ];

  function defaultFields() {
    return {
      identity: '',
      contact: { email: '', phone: '', location: '', linkedin: '' },
      voice: { do: DEFAULT_DO.slice(), dont: DEFAULT_DONT.slice() },
      stories: [],
      preferences: { open_to: '', target_roles: '', strongest_fit: '', not_interested_in: '' },
      additional_notes: '',
    };
  }

  function cloneFields(f) {
    return JSON.parse(JSON.stringify(f));
  }

  function mergedFields(fields) {
    var d = defaultFields();
    fields = fields || {};
    var voiceDo = fields.voice && fields.voice.do;
    var voiceDont = fields.voice && fields.voice.dont;
    return {
      identity: fields.identity || '',
      contact: Object.assign(d.contact, fields.contact),
      voice: {
        do: (voiceDo && voiceDo.length) ? voiceDo.slice() : d.voice.do,
        dont: (voiceDont && voiceDont.length) ? voiceDont.slice() : d.voice.dont,
      },
      stories: (fields.stories || []).slice(),
      preferences: Object.assign(d.preferences, fields.preferences),
      additional_notes: fields.additional_notes || '',
    };
  }

  // ---------------------------------------------------------------------
  // Markdown <-> fields
  // ---------------------------------------------------------------------

  function fieldsToMarkdown(fields) {
    var f = mergedFields(fields);
    var lines = ['# Candidate Profile & Voice Guide', ''];

    if (f.identity.trim()) {
      lines.push('## Identity & Positioning', f.identity.trim(), '');
    }

    var contactLines = [];
    if (f.contact.email) contactLines.push('- Email: ' + f.contact.email);
    if (f.contact.phone) contactLines.push('- Phone: ' + f.contact.phone);
    if (f.contact.location) contactLines.push('- Location: ' + f.contact.location);
    if (f.contact.linkedin) contactLines.push('- LinkedIn: ' + f.contact.linkedin);
    if (contactLines.length) {
      lines.push('## Contact');
      lines = lines.concat(contactLines);
      lines.push('');
    }

    var doItems = (f.voice.do || []).filter(function (s) { return s && s.trim(); });
    var dontItems = (f.voice.dont || []).filter(function (s) { return s && s.trim(); });
    if (doItems.length || dontItems.length) {
      lines.push('## Voice & Tone Rules');
      if (doItems.length) {
        lines.push('**DO:**');
        doItems.forEach(function (i) { lines.push('- ' + i.trim()); });
      }
      if (dontItems.length) {
        if (doItems.length) lines.push('');
        lines.push('**DO NOT:**');
        dontItems.forEach(function (i) { lines.push('- ' + i.trim()); });
      }
      lines.push('');
    }

    var stories = (f.stories || []).filter(function (s) {
      return (s.title && s.title.trim()) || (s.description && s.description.trim());
    });
    if (stories.length) {
      lines.push('## Key Stories & Proof Points');
      stories.forEach(function (s) {
        var title = (s.title || '').trim();
        var desc = (s.description || '').trim();
        lines.push(title ? ('- **' + title + ':** ' + desc) : ('- ' + desc));
      });
      lines.push('');
    }

    var prefLines = [];
    if (f.preferences.open_to) prefLines.push('- Open to: ' + f.preferences.open_to);
    if (f.preferences.target_roles) prefLines.push('- Target roles: ' + f.preferences.target_roles);
    if (f.preferences.strongest_fit) prefLines.push('- Strongest fit: ' + f.preferences.strongest_fit);
    if (f.preferences.not_interested_in) prefLines.push('- Not interested in: ' + f.preferences.not_interested_in);
    if (prefLines.length) {
      lines.push('## Preferences');
      lines = lines.concat(prefLines);
      lines.push('');
    }

    if (f.additional_notes.trim()) {
      lines.push('## Additional Notes', f.additional_notes.trim(), '');
    }

    return lines.join('\n').trim() + '\n';
  }

  function markdownToFields(markdown) {
    var fields = defaultFields(); // voice.do/dont start as the sensible defaults
    if (!markdown || !markdown.trim()) return fields;

    var lines = markdown.replace(/\r\n/g, '\n').split('\n');
    var sections = [];
    var current = null;
    lines.forEach(function (line) {
      var m = /^##\s+(.+?)\s*$/.exec(line);
      if (m) {
        current = { title: m[1], lines: [] };
        sections.push(current);
      } else if (/^#\s+/.test(line)) {
        // top-level document title — ignore
      } else if (current) {
        current.lines.push(line);
      } else if (line.trim()) {
        if (!sections.length || sections[0].title !== '__preamble__') {
          sections.unshift({ title: '__preamble__', lines: [] });
        }
        sections[0].lines.push(line);
      }
    });

    var leftover = [];

    sections.forEach(function (sec) {
      var t = sec.title.toLowerCase();
      var body = sec.lines.join('\n');
      if (sec.title === '__preamble__') {
        if (body.trim()) leftover.push(body.trim());
        return;
      }
      if (t.indexOf('identity') !== -1 || t.indexOf('positioning') !== -1) {
        fields.identity = body.trim();
      } else if (t.indexOf('contact') !== -1) {
        sec.lines.forEach(function (line) {
          var bm = /^[-*]\s*([^:]+):\s*(.+)$/.exec(line.trim());
          if (!bm) return;
          var label = bm[1].toLowerCase();
          var value = bm[2].trim();
          if (label.indexOf('email') !== -1) fields.contact.email = value;
          else if (label.indexOf('phone') !== -1) fields.contact.phone = value;
          else if (label.indexOf('location') !== -1) fields.contact.location = value;
          else if (label.indexOf('linkedin') !== -1) fields.contact.linkedin = value;
        });
      } else if (t.indexOf('voice') !== -1 || t.indexOf('tone') !== -1) {
        // Replace the defaults wholesale with whatever this section actually
        // contains, rather than appending after them.
        fields.voice.do = [];
        fields.voice.dont = [];
        var bucket = null;
        sec.lines.forEach(function (line) {
          var trimmed = line.trim();
          if (/^\*\*do\s*not:?\*\*$/i.test(trimmed) || /^\*\*don.t:?\*\*$/i.test(trimmed)) { bucket = 'dont'; return; }
          if (/^\*\*do:?\*\*$/i.test(trimmed)) { bucket = 'do'; return; }
          var bm = /^[-*]\s*(.+)$/.exec(trimmed);
          if (bm) fields.voice[bucket || 'do'].push(bm[1].trim());
        });
      } else if (t.indexOf('stories') !== -1 || t.indexOf('proof') !== -1) {
        sec.lines.forEach(function (line) {
          var bm = /^[-*]\s*(.+)$/.exec(line.trim());
          if (!bm) return;
          var content = bm[1];
          var tm = /^\*\*(.+?):?\*\*\s*(.*)$/.exec(content);
          if (tm) fields.stories.push({ title: tm[1].trim(), description: tm[2].trim() });
          else fields.stories.push({ title: '', description: content.trim() });
        });
      } else if (t.indexOf('preference') !== -1) {
        sec.lines.forEach(function (line) {
          var bm = /^[-*]\s*([^:]+):\s*(.+)$/.exec(line.trim());
          if (!bm) return;
          var label = bm[1].toLowerCase();
          var value = bm[2].trim();
          if (label.indexOf('open') !== -1) fields.preferences.open_to = value;
          else if (label.indexOf('target') !== -1 || label.indexOf('role') !== -1) fields.preferences.target_roles = value;
          else if (label.indexOf('strongest') !== -1 || label.indexOf('fit') !== -1) fields.preferences.strongest_fit = value;
          else if (label.indexOf('not interested') !== -1 || label.indexOf('exclude') !== -1) fields.preferences.not_interested_in = value;
        });
      } else if (t.indexOf('additional notes') !== -1) {
        if (body.trim()) leftover.push(body.trim());
      } else {
        // Unrecognized section (Career Timeline, Skills & Technologies, custom, etc.)
        // — preserved verbatim rather than silently dropped.
        if (body.trim()) leftover.push('## ' + sec.title + '\n' + body.trim());
      }
    });

    if (!fields.voice.do.length && !fields.voice.dont.length) {
      fields.voice.do = DEFAULT_DO.slice();
      fields.voice.dont = DEFAULT_DONT.slice();
    }
    fields.additional_notes = leftover.filter(Boolean).join('\n\n');
    return fields;
  }

  // ---------------------------------------------------------------------
  // DOM helper
  // ---------------------------------------------------------------------

  function h(tag, attrs) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k.indexOf('on') === 0 && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var child = arguments[i];
      if (child == null) continue;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return node;
  }

  // ---------------------------------------------------------------------
  // Step builders
  // ---------------------------------------------------------------------

  function buildIdentityStep(state, ctx) {
    var wrap = h('div', { class: 'pw-step' });
    wrap.appendChild(h('p', { class: 'pw-hint' },
      'A 3–5 sentence paragraph on who you are professionally and what makes you different. This opens your voice guide.'));

    if (state.aiEnabled) {
      var status = h('span', { class: 'pw-ai-status' });
      var btn = h('button', { type: 'button', class: 'pw-btn pw-btn-ghost', text: '✨ Suggest a draft' });
      btn.addEventListener('click', function () {
        if (state.fields.identity.trim() && !window.confirm('Replace your current draft with a new AI suggestion?')) return;
        btn.disabled = true; btn.textContent = 'Thinking…'; status.textContent = ''; status.classList.remove('pw-ai-error');
        state.fetchFn('/api/profile/suggest', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ section: 'identity' }),
        }).then(function (res) {
          return res.json().then(function (body) {
            if (!res.ok) throw new Error(body.detail || 'Suggestion failed.');
            state.fields.identity = body.suggestion || '';
            ctx.rerender();
          });
        }).catch(function (e) {
          status.textContent = e.message || 'Suggestion failed.';
          status.classList.add('pw-ai-error');
        }).finally(function () {
          btn.disabled = false; btn.textContent = '✨ Suggest a draft';
        });
      });
      wrap.appendChild(h('div', { class: 'pw-ai-row' }, btn, status));
    }

    var ta = h('textarea', {
      class: 'pw-textarea', rows: '6',
      placeholder: 'e.g. Senior-level technologist with 10+ years designing and delivering AI-powered solutions, API-driven integrations, and agentic workflows...',
    });
    ta.value = state.fields.identity;
    ta.addEventListener('input', function () { state.fields.identity = ta.value; ctx.emitChange(); });
    wrap.appendChild(ta);
    return wrap;
  }

  function buildContactStep(state, ctx) {
    var wrap = h('div', { class: 'pw-step' });
    wrap.appendChild(h('p', { class: 'pw-hint' }, 'How should employers reach you? Email is pulled from your account automatically.'));
    [
      { key: 'email', label: 'Email', placeholder: 'jane@example.com' },
      { key: 'phone', label: 'Phone', placeholder: '(978) 790-4272' },
      { key: 'location', label: 'Location', placeholder: 'City, State' },
      { key: 'linkedin', label: 'LinkedIn', placeholder: 'https://www.linkedin.com/in/janedoe/' },
    ].forEach(function (fd) {
      var input = h('input', { type: 'text', class: 'pw-input', placeholder: fd.placeholder });
      input.value = state.fields.contact[fd.key] || '';
      input.addEventListener('input', function () { state.fields.contact[fd.key] = input.value; ctx.emitChange(); });
      wrap.appendChild(h('div', { class: 'pw-field' }, h('label', { text: fd.label }), input));
    });
    return wrap;
  }

  function buildEditableList(items, placeholder, onEdit, onRemove) {
    var list = h('div', { class: 'pw-list' });
    items.forEach(function (item, idx) {
      var input = h('input', { type: 'text', class: 'pw-input', placeholder: placeholder });
      input.value = item;
      input.addEventListener('input', function () { onEdit(idx, input.value); });
      var rm = h('button', { type: 'button', class: 'pw-btn-icon', text: '✕', title: 'Remove' });
      rm.addEventListener('click', function () { onRemove(idx); });
      list.appendChild(h('div', { class: 'pw-list-row' }, input, rm));
    });
    return list;
  }

  function buildVoiceStep(state, ctx) {
    var wrap = h('div', { class: 'pw-step' });
    wrap.appendChild(h('p', { class: 'pw-hint' },
      'Rules that keep the AI writing like you, not like a LinkedIn summary. Sensible defaults are prefilled — edit freely.'));

    wrap.appendChild(h('label', { class: 'pw-sublabel', text: 'DO' }));
    wrap.appendChild(buildEditableList(
      state.fields.voice.do, 'e.g. Write in first person, direct and conversational',
      function (idx, val) { state.fields.voice.do[idx] = val; ctx.emitChange(); },
      function (idx) { state.fields.voice.do.splice(idx, 1); ctx.emitChange(); ctx.rerender(); }
    ));
    var addDo = h('button', { type: 'button', class: 'pw-btn pw-btn-ghost pw-btn-sm', text: '+ Add rule' });
    addDo.addEventListener('click', function () { state.fields.voice.do.push(''); ctx.emitChange(); ctx.rerender(); });
    wrap.appendChild(addDo);

    wrap.appendChild(h('label', { class: 'pw-sublabel', style: 'margin-top:1.25rem', text: "DON'T" }));
    wrap.appendChild(buildEditableList(
      state.fields.voice.dont, 'e.g. "passion for", "leverage", "synergy"',
      function (idx, val) { state.fields.voice.dont[idx] = val; ctx.emitChange(); },
      function (idx) { state.fields.voice.dont.splice(idx, 1); ctx.emitChange(); ctx.rerender(); }
    ));
    var addDont = h('button', { type: 'button', class: 'pw-btn pw-btn-ghost pw-btn-sm', text: '+ Add rule' });
    addDont.addEventListener('click', function () { state.fields.voice.dont.push(''); ctx.emitChange(); ctx.rerender(); });
    wrap.appendChild(addDont);

    return wrap;
  }

  function buildStoriesStep(state, ctx) {
    var wrap = h('div', { class: 'pw-step' });
    wrap.appendChild(h('p', { class: 'pw-hint' },
      'Your strongest, most specific achievements — the proof points a resume or cover letter can cite later. Numbers and system names beat vague claims.'));

    if (state.aiEnabled) {
      var status = h('span', { class: 'pw-ai-status' });
      var btn = h('button', { type: 'button', class: 'pw-btn pw-btn-ghost', text: '✨ Suggest stories from my resume' });
      btn.addEventListener('click', function () {
        btn.disabled = true; btn.textContent = 'Reading your resume…'; status.textContent = ''; status.classList.remove('pw-ai-error');
        state.fetchFn('/api/profile/suggest', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ section: 'stories' }),
        }).then(function (res) {
          return res.json().then(function (body) {
            if (!res.ok) throw new Error(body.detail || 'Suggestion failed.');
            (body.suggestions || []).forEach(function (s) {
              state.fields.stories.push({ title: s.title || '', description: s.description || '' });
            });
            ctx.emitChange(); ctx.rerender();
          });
        }).catch(function (e) {
          status.textContent = e.message || 'Suggestion failed.';
          status.classList.add('pw-ai-error');
        }).finally(function () {
          btn.disabled = false; btn.textContent = '✨ Suggest stories from my resume';
        });
      });
      wrap.appendChild(h('div', { class: 'pw-ai-row' }, btn, status));
    }

    var list = h('div', { class: 'pw-cards' });
    state.fields.stories.forEach(function (story, idx) {
      var titleInput = h('input', { type: 'text', class: 'pw-input', placeholder: 'e.g. HAL chatbot (eHealth)' });
      titleInput.value = story.title || '';
      titleInput.addEventListener('input', function () { story.title = titleInput.value; ctx.emitChange(); });
      var rm = h('button', { type: 'button', class: 'pw-btn-icon', text: '✕', title: 'Remove' });
      rm.addEventListener('click', function () { state.fields.stories.splice(idx, 1); ctx.emitChange(); ctx.rerender(); });
      var descTa = h('textarea', {
        class: 'pw-textarea', rows: '2',
        placeholder: 'What did you build/ship/deliver, and what was the measurable outcome?',
      });
      descTa.value = story.description || '';
      descTa.addEventListener('input', function () { story.description = descTa.value; ctx.emitChange(); });
      list.appendChild(h('div', { class: 'pw-card' }, h('div', { class: 'pw-card-head' }, titleInput, rm), descTa));
    });
    wrap.appendChild(list);

    var addBtn = h('button', { type: 'button', class: 'pw-btn pw-btn-ghost pw-btn-sm', text: '+ Add a story' });
    addBtn.addEventListener('click', function () { state.fields.stories.push({ title: '', description: '' }); ctx.emitChange(); ctx.rerender(); });
    wrap.appendChild(addBtn);
    return wrap;
  }

  function buildPreferencesStep(state, ctx) {
    var wrap = h('div', { class: 'pw-step' });
    wrap.appendChild(h('p', { class: 'pw-hint' }, 'What you’re looking for — helps the AI frame cover letters and know what to steer away from.'));
    [
      { key: 'open_to', label: 'Open to', placeholder: 'Remote or hybrid (based in Sterling, MA)' },
      { key: 'target_roles', label: 'Target roles', placeholder: 'Solutions Engineer, Integration Engineer, TAM' },
      { key: 'strongest_fit', label: 'Strongest fit', placeholder: 'iPaaS, agentic AI, SaaS platforms' },
      { key: 'not_interested_in', label: 'Not interested in', placeholder: 'Pure sales roles, fully on-site positions' },
    ].forEach(function (fd) {
      var input = h('input', { type: 'text', class: 'pw-input', placeholder: fd.placeholder });
      input.value = state.fields.preferences[fd.key] || '';
      input.addEventListener('input', function () { state.fields.preferences[fd.key] = input.value; ctx.emitChange(); });
      wrap.appendChild(h('div', { class: 'pw-field' }, h('label', { text: fd.label }), input));
    });
    return wrap;
  }

  function buildReviewStep(state) {
    var wrap = h('div', { class: 'pw-step' });
    wrap.appendChild(h('p', { class: 'pw-hint' }, 'This is what gets saved as your Profile & Voice Guide. Go back to adjust any section.'));
    var md = fieldsToMarkdown(state.fields);
    var preview = h('div', { class: 'pw-preview' });
    if (typeof marked !== 'undefined') {
      var html = marked.parse(md.trim() ? md : '*Nothing yet — go back and fill in a section.*');
      preview.innerHTML = typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(html) : html;
    } else {
      preview.textContent = md;
    }
    wrap.appendChild(preview);
    return wrap;
  }

  var STEP_DEFS = [
    { key: 'identity', label: 'Identity', build: buildIdentityStep },
    { key: 'contact', label: 'Contact', build: buildContactStep },
    { key: 'voice', label: 'Voice & Tone', build: buildVoiceStep },
    { key: 'stories', label: 'Key Stories', build: buildStoriesStep },
    { key: 'preferences', label: 'Preferences', build: buildPreferencesStep },
    { key: 'review', label: 'Review', build: buildReviewStep },
  ];

  function mount(container, opts) {
    opts = opts || {};
    injectStyles();

    var state = {
      step: 0,
      fields: mergedFields(opts.fields),
      aiEnabled: !!opts.aiEnabled,
      embedded: !!opts.embedded,
      fetchFn: opts.fetchFn || window.fetch.bind(window),
    };
    if (opts.email && !state.fields.contact.email) state.fields.contact.email = opts.email;

    var onSave = opts.onSave || function () {};
    var onCancel = opts.onCancel || function () {};
    var onChange = opts.onChange || function () {};

    function emitChange() {
      onChange(fieldsToMarkdown(state.fields), cloneFields(state.fields));
    }

    function render() {
      container.innerHTML = '';
      var root = h('div', { class: 'pw-root' });

      var stepper = h('div', { class: 'pw-stepper' });
      STEP_DEFS.forEach(function (def, i) {
        var cls = 'pw-step-dot' + (i === state.step ? ' pw-active' : '') + (i < state.step ? ' pw-done' : '');
        var dot = h('div', { class: cls }, h('span', { class: 'pw-step-num', text: String(i + 1) }), h('span', { text: def.label }));
        dot.addEventListener('click', function () { state.step = i; render(); });
        stepper.appendChild(dot);
      });
      root.appendChild(stepper);

      var ctx = { rerender: render, emitChange: emitChange };
      var body = h('div', { class: 'pw-body' }, STEP_DEFS[state.step].build(state, ctx));
      root.appendChild(body);

      var nav = h('div', { class: 'pw-nav' });
      if (state.step > 0) {
        nav.appendChild(h('button', {
          type: 'button', class: 'pw-btn pw-btn-ghost', text: '← Back',
          onclick: function () { state.step--; render(); },
        }));
      } else {
        nav.appendChild(h('span'));
      }
      if (state.step < STEP_DEFS.length - 1) {
        nav.appendChild(h('button', {
          type: 'button', class: 'pw-btn', text: 'Next →',
          onclick: function () { state.step++; render(); },
        }));
      } else if (!state.embedded) {
        nav.appendChild(h('div', { class: 'pw-btn-row' },
          h('button', { type: 'button', class: 'pw-btn pw-btn-ghost', text: 'Cancel', onclick: function () { onCancel(); } }),
          h('button', { type: 'button', class: 'pw-btn', text: 'Save', onclick: function () { onSave(fieldsToMarkdown(state.fields), cloneFields(state.fields)); } })
        ));
      }
      root.appendChild(nav);

      container.appendChild(root);
    }

    render();
    emitChange();

    return {
      getMarkdown: function () { return fieldsToMarkdown(state.fields); },
      getFields: function () { return cloneFields(state.fields); },
      setFields: function (f) { state.fields = mergedFields(f); state.step = 0; render(); emitChange(); },
      destroy: function () { container.innerHTML = ''; },
    };
  }

  window.ProfileWizard = {
    mount: mount,
    fieldsToMarkdown: fieldsToMarkdown,
    markdownToFields: markdownToFields,
    defaultFields: defaultFields,
  };
})();
