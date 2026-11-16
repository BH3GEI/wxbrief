(function(){var box=document.getElementById('botfold');if(!box)return;
var list=document.getElementById('botlist'),badge=document.getElementById('botnew');
function get(k){try{return localStorage.getItem(k)}catch(e){return null}}
function set(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function show(open){list.hidden=!open;box.classList.toggle('open',open);set('wxb_fold',open?'1':'0')}
show(get('wxb_fold')==='1');
box.onclick=function(){show(list.hidden)};
var n=0;[].forEach.call(list.querySelectorAll('[data-last]'),function(a){
 var last=+a.dataset.last,seen=+(get('wxb_seen_'+a.dataset.chat)||0);
 if(last>seen){n++;a.classList.add('unread')}});
if(n){badge.textContent=n;badge.hidden=false}
})();
