(function(){
function get(k){try{return localStorage.getItem(k)}catch(e){return null}}
function set(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function fold(head,body,key){
 if(!head||!body)return;
 function show(open){body.hidden=!open;head.classList.toggle('open',open);set(key,open?'1':'0')}
 show(get(key)==='1');
 head.onclick=function(){show(body.hidden)};
}
var list=document.getElementById('botlist');if(!list)return;
fold(document.getElementById('botfold'),list,'wxb_fold');
fold(document.getElementById('closedfold'),document.getElementById('closedlist'),'wxb_fold_closed');
var n=0,badge=document.getElementById('botnew');
[].forEach.call(list.querySelectorAll('[data-last]'),function(a){
 var last=+a.dataset.last,seen=+(get('wxb_seen_'+a.dataset.chat)||0);
 if(last>seen){n++;a.classList.add('unread')}});
if(n){badge.textContent=n;badge.hidden=false}
})();
