// Standalone design fixture, deliberately not connected to AuditLab API.
const rows = [
 ['Компания B','Ноябрь 2026','finding','Есть вывод'],
 ['Компания A','Ноябрь 2026','clear','Без выводов'],
 ['Частичное распределение','Проверка платежей','finding','Есть вывод'],
 ['Полное распределение','Проверка платежей','clear','Без выводов'],
 ['Граница периода','Проверка платежей','unknown','Не проверено'],
];
const search=document.querySelector('#search');
const dialog=document.querySelector('#notice');
let active='all';
function explain(text){document.querySelector('#notice-text').textContent=text;dialog.showModal();}
function render(){
 const query=search.value.toLocaleLowerCase('ru').trim();
 const shown=rows.filter(r=>(active==='all'||r[2]===active)&&`${r[0]} ${r[1]} Сверка платежей`.toLocaleLowerCase('ru').includes(query));
 document.querySelector('#run-rows').innerHTML=shown.map(r=>`<tr><td><div class="run-name"><svg aria-hidden="true"><use href="#i-building"/></svg><span><b>${r[0]}</b><small>${r[1]}</small></span></div></td><td>Сверка платежей</td><td><span class="status ${r[2]}"><i class="dot" aria-hidden="true"></i>${r[3]}</span></td><td><button class="row-link" aria-label="Открыть: ${r[0]}"><svg aria-hidden="true"><use href="#i-arrow"/></svg></button></td></tr>`).join('');
 document.querySelector('#empty').hidden=shown.length>0;
 document.querySelector('#count').textContent=`Показано: ${shown.length}`;
 document.querySelectorAll('.row-link').forEach(b=>b.addEventListener('click',()=>explain('В приложении эта строка открывает соответствующий запуск. Значения образца не являются результатами текущего API.')));
}
document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{active=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));render();}));
search.addEventListener('input',render);
document.querySelectorAll('[data-prototype]').forEach(b=>b.addEventListener('click',()=>explain(b.dataset.prototype)));
function reset(){search.value='';document.querySelector('[data-filter=all]').click();}
document.querySelector('#reset').addEventListener('click',reset);
document.querySelector('#all-runs').addEventListener('click',()=>{reset();document.querySelector('#recent').scrollIntoView();});
document.querySelector('#open-run').addEventListener('click',()=>{document.querySelector('#recent').scrollIntoView();search.focus({preventScroll:true});});
document.querySelector('.mobile-menu').addEventListener('click',()=>explain('В приложении эта кнопка открывает мобильную навигацию. В этом образце показан только главный экран.'));
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'&&!dialog.open){e.preventDefault();search.focus();}});
// Decorative icon labels are provided by the surrounding controls/text.
document.querySelectorAll('svg').forEach(s=>s.setAttribute('aria-hidden','true'));
render();
