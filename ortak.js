/* Çağrı Merkezi paneli — ortak menü */
(function(){
  const I={
    takip:'<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
    kayit:'<path d="M3 5h18M3 12h18M3 19h18"/><circle cx="7" cy="5" r=".5"/>',
    bilgi:'<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    onay:'<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M12 18h.01"/>',
    ses:'<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/>',
    disa:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5M12 15V3"/>',
    cikis:'<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/>',
    menu:'<path d="M3 6h18M3 12h18M3 18h18"/>',
    logo:'<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/>'
  };
  const svg=k=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${I[k]}</svg>`;
  const mobil=()=>window.matchMedia('(max-width:900px)').matches;
  const P=location.pathname.replace(/\/+$/,'')||'/';
  const L=[
    ['Günlük iş',[
      ['/takip','Takip listesi','takip',p=>p==='/takip'],
      ['/kayitlar','Görüşme kayıtları','kayit',p=>p==='/kayitlar'],
    ]],
    ['Yapay zekâ agent',[
      ['/bilgi','Bilgi tabanı','bilgi',p=>p==='/bilgi'],
      ['/bilgi/mobil','Tek tek onay','onay',p=>p==='/bilgi/mobil'],
    ]],
    ['Araçlar',[
      ['/','Ses → metin','ses',p=>p==='/'],
      ['/admin/kb/export','Bilgi tabanı (zip)','disa',()=>false],
    ]],
  ];
  let h=`<div class="brand"><div class="logo">${svg('logo')}</div><div><b>Çağrı Merkezi</b><span>Analiz paneli</span></div></div>`;
  for(const [sec,items] of L){
    h+=`<div class="navsec">${sec}</div>`;
    for(const [href,ad,ic,akt] of items)h+=`<a class="navlink${akt(P)?' on':''}" href="${href}" data-k="${ic}">${svg(ic)}<span>${ad}</span></a>`;
  }
  h+=`<div class="navfoot"><a class="navlink" href="/logout">${svg('cikis')}<span>Çıkış yap</span></a></div>`;
  const nav=document.createElement('aside'); nav.className='sidenav'; nav.innerHTML=h;
  const scrim=document.createElement('div'); scrim.className='scrim';
  document.body.prepend(scrim); document.body.prepend(nav); document.body.classList.add('ui');
  const kapat=()=>document.body.classList.remove('nav-open');
  scrim.onclick=kapat; nav.addEventListener('click',e=>{if(e.target.closest('a'))kapat()});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')kapat()});
  const top=document.querySelector('.topbar');
  if(top){const b=document.createElement('button');b.className='menubtn';b.type='button';b.setAttribute('aria-label','Menü');b.innerHTML=svg('menu');
    b.onclick=()=>document.body.classList.toggle('nav-open');top.prepend(b)}
  // Bilgi tabanı linki mobilde tek tek onay moduna gitsin
  if(mobil()){const a=nav.querySelector('[data-k="bilgi"]');if(a&&P!=='/bilgi')a.href='/bilgi/mobil'}
  // Takip: açık satır sayısı
  fetch('/admin/takip/liste?limit=1').then(r=>r.ok?r.json():[]).then(l=>{
    const n=(l[0]||{}).acik; if(!n)return;
    const a=nav.querySelector('[data-k="takip"]'); if(a)a.insertAdjacentHTML('beforeend',`<span class="cnt" title="Son listede açık satır">${n}</span>`);
  }).catch(()=>{});
})();
