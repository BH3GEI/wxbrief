(function(){var q='__QUERY__',v0=null,chat=q.indexOf('tab=chat')===0,fast=q.indexOf('fast=1')>-1;
function on(){try{return localStorage.getItem('wxb_auto')!=='0'}catch(e){return true}}
function atBottom(){return window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-120}
function busy(){return window.__leaving||document.visibilityState!=='visible'||document.querySelector('button.armed')||document.querySelector('details[open]')||
 [].some.call(document.querySelectorAll('input:not([type=hidden]):not([type=submit]),textarea'),function(i){return i.value})}
/* Lists (WeChat tab, Telegram tab): reload once something changed and the user is not in the middle of something.
   Chat pages do not reload at all: sync.js adds new messages in place. */
function tick(){if(window.__leaving||!on()||document.visibilityState!=='visible')return;
 fetch('/ping?'+q,{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
  if(v0===null){v0=d.v;return}
  if(d.v!==v0&&!busy())location.reload()}).catch(function(){})}
if(!chat){setInterval(tick,fast?2000:10000);tick()}
var t=document.getElementById('autot');
if(t){var lab=function(){t.textContent='自动刷新：'+(on()?'开':'关');t.classList.toggle('on',on())};lab();
 t.onclick=function(){try{localStorage.setItem('wxb_auto',on()?'0':'1')}catch(e){}lab()}}
if(chat){var stick=true,down=function(){window.scrollTo(0,document.documentElement.scrollHeight)},
 hit=location.hash.length>1&&document.getElementById(location.hash.slice(1));
 if('scrollRestoration' in history)history.scrollRestoration='manual';
 if(hit){stick=false;hit.classList.add('hl');hit.scrollIntoView({block:'center'});window.addEventListener('load',function(){hit.scrollIntoView({block:'center'})})}
 else{down();window.addEventListener('load',down);
  [].forEach.call(document.images,function(i){if(!i.complete)i.addEventListener('load',function(){if(stick)down()})})}
 window.addEventListener('wheel',function(e){if(e.deltaY<0)stick=false},{passive:true});
 window.addEventListener('touchmove',function(){setTimeout(function(){stick=atBottom()},50)},{passive:true});
 window.addEventListener('keydown',function(e){if(/^(PageUp|ArrowUp|Home)$/.test(e.key))stick=false});
 window.addEventListener('scroll',function(){if(atBottom())stick=true},{passive:true})}
})()
