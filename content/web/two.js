document.querySelectorAll('form.two').forEach(function(f){var b=f.querySelector('button'),o=b.textContent,tm;
f.addEventListener('submit',function(ev){if(f.dataset.armed){return}ev.preventDefault();f.dataset.armed='1';b.textContent=b.dataset.ask;b.classList.add('armed');
clearTimeout(tm);tm=setTimeout(function(){delete f.dataset.armed;b.textContent=o;b.classList.remove('armed')},5000)})});
