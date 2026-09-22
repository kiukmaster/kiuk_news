/* Progressive enhancement only. Report content is already in the generated HTML. */
(() => {
  'use strict';
  const body = document.body;
  const keepDays = Number(body.dataset.keepDays || 5);
  const dateParts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit'
  }).formatToParts(new Date());
  const getPart = type => dateParts.find(p => p.type === type).value;
  const today = `${getPart('year')}-${getPart('month')}-${getPart('day')}`;
  const cutoff = new Date(`${today}T00:00:00Z`);
  cutoff.setUTCDate(cutoff.getUTCDate() - keepDays + 1);
  const cutoffDate = cutoff.toISOString().slice(0, 10);
  const scriptPath = Array.from(document.scripts).find(s => s.src.includes('/assets/app.js'))?.src;
  const basePath = scriptPath ? new URL('../', scriptPath).pathname : '/';
  const storageKey = `daily-digest:bookmarks:${basePath}`;
  let saved = new Set();
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '[]');
    if (Array.isArray(value)) saved = new Set(value.filter(d => typeof d === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(d) && d >= cutoffDate));
  } catch (_) { /* Browser privacy mode can block storage. Content remains readable. */ }
  const notify = message => {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(notify.timer);
    notify.timer = setTimeout(() => { toast.hidden = true; }, 3200);
  };
  document.querySelectorAll('[data-js-only]').forEach(el => { el.hidden = false; });
  const reportDate = body.dataset.reportDate;
  const expired = reportDate && reportDate < cutoffDate;
  if (expired) {
    const content = document.getElementById('report-content');
    if (content) content.hidden = true;
    const notice = document.getElementById('expired-report');
    if (notice) notice.hidden = false;
  }
  const bookmark = document.querySelector('[data-bookmark]');
  const renderBookmark = () => {
    if (!bookmark) return;
    bookmark.setAttribute('aria-pressed', String(saved.has(reportDate)));
    bookmark.setAttribute('aria-label', saved.has(reportDate) ? '보고서 저장 해제' : '보고서 저장');
  };
  if (bookmark) {
    renderBookmark();
    bookmark.addEventListener('click', () => {
      if (expired) return notify('보관 기간이 지난 보고서는 저장할 수 없습니다.');
      const next = new Set(saved);
      if (next.has(reportDate)) next.delete(reportDate); else next.add(reportDate);
      try {
        localStorage.setItem(storageKey, JSON.stringify([...next]));
        saved = next;
        renderBookmark();
        notify(saved.has(reportDate) ? '이 브라우저에 저장했습니다. 보관 기간은 연장되지 않습니다.' : '저장을 해제했습니다.');
      } catch (_) { notify('브라우저 설정 때문에 저장할 수 없습니다.'); }
    });
  }
  document.querySelector('[data-share]')?.addEventListener('click', async () => {
    try {
      if (navigator.share) {
        await navigator.share({ title: document.title, url: location.href });
        return;
      }
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(location.href);
        notify('보고서 주소를 복사했습니다.');
      } else {
        notify('이 환경에서는 주소창의 URL을 복사해 공유해 주세요.');
      }
    } catch (error) {
      if (error.name !== 'AbortError') notify('공유하지 못했습니다. 주소창의 URL을 복사해 주세요.');
    }
  });
  const archiveCards = [...document.querySelectorAll('[data-report-card]')];
  function filterArchive(filter) {
    let visible = 0;
    for (const card of archiveCards) {
      const date = card.dataset.date;
      card.hidden = date < cutoffDate || (filter === 'saved' && !saved.has(date));
      visible += Number(!card.hidden);
      const marker = card.querySelector('[data-saved-icon]');
      if (marker) marker.hidden = !saved.has(date);
    }
    const empty = document.getElementById('filter-empty');
    if (empty) {
      empty.hidden = visible > 0;
      if (filter === 'all' && visible === 0) {
        empty.querySelector('h2').textContent = '보관 중인 보고서가 없습니다';
        empty.querySelector('p').textContent = '새 보고서 수집이 완료되면 여기에 표시됩니다.';
      } else {
        empty.querySelector('h2').textContent = '저장한 보고서가 없습니다';
        empty.querySelector('p').textContent = '보고서 상단의 저장 버튼을 누르면 이 기기에서 모아 볼 수 있습니다.';
      }
    }
    document.querySelectorAll('[data-archive-filter]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.archiveFilter === filter)));
  }
  if (archiveCards.length) filterArchive('all');
  document.querySelectorAll('[data-archive-filter]').forEach(b => b.addEventListener('click', () => filterArchive(b.dataset.archiveFilter)));
  const search = document.getElementById('issue-search');
  const issueCards = [...document.querySelectorAll('[data-issue-card]')];
  const searchable = issueCards.map(card => ({ card, text: card.textContent.toLocaleLowerCase('ko-KR') }));
  const cveMore = document.querySelector('[data-cve-more]');
  let cveLimit = 20;
  function filterIssues() {
    const query = (search?.value || '').trim().toLocaleLowerCase('ko-KR');
    let count = 0, cveCount = 0;
    for (const {card, text} of searchable) {
      const matches = !query || text.includes(query);
      const isCve = card.hasAttribute('data-cve-card');
      if (isCve && matches) cveCount += 1;
      // Search always covers ALL CVEs, including cards not yet expanded.
      card.hidden = !matches || (isCve && !query && cveCount > cveLimit);
      count += Number(!card.hidden);
    }
    if (cveMore) {
      const remaining = Math.max(0, cveCount - cveLimit);
      cveMore.hidden = !!query || remaining === 0;
      cveMore.textContent = `다음 CVE ${Math.min(20, remaining)}건 보기 · 남은 ${remaining}건`;
    }
    document.querySelectorAll('[data-section]').forEach(section => {
      const hasVisible = [...section.querySelectorAll('[data-issue-card]')].some(card => !card.hidden);
      const message = section.querySelector('.search-empty');
      if (message) message.hidden = !query || hasVisible;
      const originalEmpty = section.querySelector('.section-empty');
      if (originalEmpty) originalEmpty.hidden = !!query;
    });
    const searchStatus = document.getElementById('search-status');
    if (searchStatus) searchStatus.textContent = query ? `${count}개 카드` : '';
    updateReading();
  }
  search?.addEventListener('input', filterIssues);
  cveMore?.addEventListener('click', () => { cveLimit += 20; filterIssues(); });
  const tabs = [...document.querySelectorAll('.nav-tab')];
  const sections = [...document.querySelectorAll('[data-section]')];
  const bar = document.getElementById('reading-progress');
  let scheduled = false;
  function updateReading() {
    if (bar) {
      const height = document.documentElement.scrollHeight - window.innerHeight;
      const ratio = height > 0 ? Math.min(1, Math.max(0, window.scrollY / height)) : 0;
      bar.style.width = `${ratio * 100}%`;
    }
    let active = sections[0]?.id;
    const threshold = (document.querySelector('.topbar')?.getBoundingClientRect().height || 64) + 90;
    for (const section of sections) {
      if (section.getBoundingClientRect().top <= threshold) active = section.id;
    }
    for (const tab of tabs) {
      const isActive = tab.hash === `#${active}`;
      const changed = tab.classList.contains('active') !== isActive;
      tab.classList.toggle('active', isActive);
      if (isActive) {
        tab.setAttribute('aria-current', 'location');
        if (changed) {
          // Scroll ONLY the horizontal nav container. Never scroll the page on tab activation.
          const parent = tab.parentElement;
          const left = tab.offsetLeft - parent.offsetLeft;
          if (left < parent.scrollLeft) parent.scrollLeft = left;
          else if (left + tab.offsetWidth > parent.scrollLeft + parent.clientWidth)
            parent.scrollLeft = left + tab.offsetWidth - parent.clientWidth;
        }
      } else tab.removeAttribute('aria-current');
    }
    scheduled = false;
  }
  const requestReading = () => {
    if (!scheduled) { scheduled = true; requestAnimationFrame(updateReading); }
  };
  window.addEventListener('scroll', requestReading, {passive: true});
  window.addEventListener('resize', requestReading, {passive: true});
  filterIssues();
})();
