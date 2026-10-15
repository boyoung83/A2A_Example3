// 파일명: keys.js — 키 장부 화면 (운영 콘솔·구매 포털 공통). 페이지에 <div id="keysPanel"></div>만 두면 그린다.
(() => {
  const root = document.getElementById('keysPanel');
  if (!root) return;
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const t = s => s ? esc(s.slice(0, 16).replace('T', ' ')) : '-';
  const LABEL = {ACTIVE: '유효', EXPIRING: '만료 예정', EXPIRED: '만료', REVOKED: '폐기'};
  const CHIP = {ACTIVE: 'c-ok', EXPIRING: 'c-wait', EXPIRED: 'c-idle', REVOKED: 'c-bad'};
  const say = (msg, kind) => (window.toast ? window.toast(msg, kind) : alert(msg));
  let modalOpen = false;

  async function api(path, body) {
    const res = await fetch(path, body === undefined ? {} :
      {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  }

  // 새 키 원문은 이 창에서 한 번만 보여 준다
  function showKey(r) {
    modalOpen = true;
    const box = document.createElement('div');
    box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:50;padding:16px';
    const old = r.old && r.old.length ? `<p>기존 키 <b>${esc(r.old.join(', '))}</b>: ${r.grace_hours > 0 ? `${r.grace_hours}시간 동안 함께 유효, 그 뒤 만료` : '즉시 폐기'}</p>` : '';
    box.innerHTML = `<div style="background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:10px;max-width:620px;width:100%;padding:16px 18px">
      <h3 style="margin:0 0 8px;font-size:15px">새 키 ${esc(r.key_id)} · ${esc(r.partner)}</h3>
      <div style="display:flex;gap:8px"><input readonly value="${esc(r.key)}" style="flex:1;font-family:Consolas,monospace;font-size:12px" id="kNew">
        <button id="kCopy">복사</button></div>
      <p style="color:var(--bad);font-size:12px;margin:8px 0">이 키 원문은 지금 한 번만 보입니다. 장부에는 해시만 남아 다시 볼 수 없습니다.</p>
      ${r.delivered ? `<p style="font-size:12px">받는 쪽 키 파일에 전달했습니다 (개발용): <span style="font-family:Consolas,monospace">${esc(r.delivered)}</span></p>`
                    : '<p style="font-size:12px">상대 회사에 안전한 경로로 전달하세요.</p>'}
      ${old}
      <div style="text-align:right;margin-top:10px"><button class="primary" id="kClose">확인했습니다</button></div></div>`;
    document.body.appendChild(box);
    box.querySelector('#kCopy').onclick = async () => {
      try { await navigator.clipboard.writeText(r.key); say('복사했습니다', 'ok'); }
      catch { box.querySelector('#kNew').select(); }
    };
    box.querySelector('#kClose').onclick = () => { box.remove(); modalOpen = false; render(); };
  }

  async function run(fn) {
    try { const r = await fn(); if (r && r.key) showKey(r); else if (r && r.revoked) say(`폐기: ${r.revoked.join(', ')} — 다음 요청부터 거절됩니다`, 'ok'); }
    catch (e) { say(e.message, 'bad'); }
    render();
  }

  async function render() {
    if (modalOpen || root.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;
    let d;
    try { d = await api('/api/keys'); } catch (e) { root.innerHTML = `<div class="muted">키 장부를 읽지 못했습니다: ${esc(e.message)}</div>`; return; }
    const partners = [...new Set(d.keys.map(k => k.partner))];
    const deliverChecked = root.querySelector('#kDeliver') ? root.querySelector('#kDeliver').checked : false;  // 기본: 전달 안 함(실제처럼)
    root.innerHTML = `
      <div class="muted" style="font-size:12px;margin-bottom:8px">${esc(d.title)} · 장부 ${esc(d.ledger)} — 원문 대신 해시만 저장, 에이전트가 요청마다 확인하므로 재시작 없이 반영</div>
      <table><tr><th>키</th><th>대상</th><th>상태</th><th>앞뒤 글자</th><th>발급</th><th>최근 사용</th><th>만료</th><th>메모</th><th></th></tr>
      ${d.keys.map(k => `<tr><td class="mono">${esc(k.key_id)}</td><td>${esc(k.partner)}</td>
        <td><span class="chip ${CHIP[k.state]}">${LABEL[k.state]}</span></td><td class="mono">${esc(k.hint)}</td>
        <td>${t(k.created_at)}</td><td>${t(k.last_used_at)}</td><td>${t(k.expires_at)}</td><td class="muted">${esc(k.note)}</td>
        <td style="white-space:nowrap">${k.state === 'ACTIVE' || k.state === 'EXPIRING' ? `<button data-revoke="${esc(k.key_id)}">폐기</button>` : ''}</td></tr>`).join('')
        || '<tr><td class="muted" colspan="9">발급한 키가 없습니다.</td></tr>'}
      </table>
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px">
        <input id="kPartner" placeholder="${esc(d.partner_hint)}" list="kPartners" style="width:160px">
        <datalist id="kPartners">${partners.map(p => `<option value="${esc(p)}">`).join('')}</datalist>
        <button id="kRotate" class="primary" title="새 키 발급 + 기존 키는 유예기간 뒤 만료">키 교체</button>
        <label class="muted" style="font-size:12px">유예 <input id="kGrace" type="number" value="24" min="0" step="1" style="width:64px"> 시간</label>
        <button id="kIssue" title="이 대상에게 키 하나를 더 발급">새 키 발급</button>
        <label class="muted" style="font-size:12px">유효 <input id="kExpires" type="number" min="1" step="1" placeholder="무기한" style="width:80px"> 시간</label>
        <button id="kRevokeAll" class="danger" title="이 대상의 유효한 키를 모두 즉시 폐기">대상 키 전부 폐기</button>
        <label class="muted" style="font-size:12px"><input type="checkbox" id="kDeliver" ${deliverChecked ? 'checked' : ''}> 새 키를 받는 쪽 키 파일에 자동 전달(개발용 — 끄면 상대는 새 키를 직접 등록해야 함)</label>
      </div>`;
    const partner = () => root.querySelector('#kPartner').value.trim();
    const deliver = () => root.querySelector('#kDeliver').checked;
    root.querySelectorAll('[data-revoke]').forEach(b => b.onclick = () => {
      const reason = prompt(`${b.dataset.revoke} 키를 즉시 폐기합니다. 사유를 적어 주세요`);
      if (reason !== null) run(() => api(`/api/keys/${encodeURIComponent(b.dataset.revoke)}/revoke`, {reason}));
    });
    root.querySelector('#kRotate').onclick = () => {
      if (!partner()) return say('대상 이름을 입력하세요', 'bad');
      const grace = root.querySelector('#kGrace').value;
      if (confirm(`${partner()}의 키를 교체합니다. 기존 키는 ${Number(grace) > 0 ? grace + '시간 뒤 만료' : '즉시 폐기'}됩니다.`))
        run(() => api('/api/keys/rotate', {partner: partner(), grace_hours: grace, deliver: deliver()}));
    };
    root.querySelector('#kIssue').onclick = () => {
      if (!partner()) return say('대상 이름을 입력하세요', 'bad');
      run(() => api('/api/keys/issue', {partner: partner(), expires_hours: root.querySelector('#kExpires').value, deliver: deliver()}));
    };
    root.querySelector('#kRevokeAll').onclick = () => {
      if (!partner()) return say('대상 이름을 입력하세요', 'bad');
      const reason = prompt(`${partner()}의 유효한 키를 모두 즉시 폐기합니다. 사유를 적어 주세요`);
      if (reason !== null) run(() => api('/api/keys/revoke-partner', {partner: partner(), reason}));
    };
  }

  render();
  setInterval(render, 10000);  // 최근 사용 시각 갱신
})();
