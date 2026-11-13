const tg=window.Telegram?.WebApp; tg?.ready();tg?.expand();
const $=id=>document.getElementById(id), money=v=>new Intl.NumberFormat('ru-RU').format(v)+' ₽';
function el(tag,attrs={},...children){const e=document.createElement(tag);for(const [k,v]of Object.entries(attrs)){if(k.startsWith('on'))e[k]=v;else if(k==='class')e.className=v;else if(k==='checked')e.checked=v;else if(v!==false&&v!=null)e.setAttribute(k,v===true?'':v)}for(const c of children.flat(Infinity))if(c!=null&&c!==false)e.append(c instanceof Node?c:document.createTextNode(String(c)));return e}
const button=(text,onclick,cls='')=>el('button',{onclick,class:cls},text);
let me,catalog=[],tab='menu',category='Все',cart=[];
try{cart=JSON.parse(localStorage.getItem('shawarma-demo-cart')||'[]');if(!Array.isArray(cart))cart=[]}catch{}
function save(){localStorage.setItem('shawarma-demo-cart',JSON.stringify(cart))}
function toast(s){$('toast').textContent=s;$('toast').style.display='block';setTimeout(()=>$('toast').style.display='none',4500)}
async function api(path) {
  if (path === '/api/shop/me') return {manager: false};
  if (path === '/api/shop/catalog') return {items: window.demoCatalog};
  throw new Error('Этот раздел доступен в Telegram Mini App.');
}
function nav(){const count=cart.reduce((s,i)=>s+i.qty,0);$('bottom').replaceChildren(...[['menu','Меню'],['cart','Корзина'+(count?' · '+count:'')]].map(([t,n])=>button(n,()=>{tab=t;render()},tab===t?'active':'')))}
function fail(e){$('app').replaceChildren(el('div',{class:'empty'},el('h2',{},'Не удалось загрузить'),el('p',{},e.message),button('Повторить',render)));}
async function render(){nav();try{if(tab==='menu')await menu();else basket()}catch(e){fail(e)}}
async function menu(){catalog=(await api('/api/shop/catalog')).items;const hero=el('section',{class:'hero'},el('div',{},el('span',{class:'eyebrow'},'СВЕЖЕЕ. СОЧНОЕ. ТВОЁ.'),el('h1',{},'Хороший день',el('br'),'начинается',el('br'),'с шаурмы.'),el('p',{},'Подрумяненный лаваш, щедрая начинка. Собери свой вкус — мы завернём.'),el('span',{class:'pill'},'Самовывоз · поесть на месте')),el('img',{src:'assets/product-1.svg',alt:'Иллюстрация шаурмы с овощами'}));
const filters=el('div',{class:'tabs'},...['Все','Шаурма','Напитки'].map(c=>button(c,()=>{category=c;menu().catch(fail)},category===c?'active':'alt')));const grid=el('div',{class:'grid'});
for(const p of catalog.filter(p=>category==='Все'||p.category===category)){const add=button(p.stock_available?'Собрать свою +':'Нет в наличии',()=>customize(p));add.disabled=!p.stock_available;grid.append(el('article',{class:'card'},el('img',{src:p.image,alt:p.name,loading:'lazy'}),el('span',{class:'small'},p.weight+' · иллюстрация'),el('h3',{},p.name),el('p',{},p.description),el('div',{class:'row'},el('b',{class:'price'},money(p.price)),el('span',{class:'available'},'Доступно: '+p.stock_available)),add))}
const actions=el('div',{class:'row'},el('span',{class:'small'},'Корзина не резервирует товар'),me.manager?el('a',{href:'/'},'Кабинет менеджера ↗'):null);$('app').replaceChildren(hero,filters,grid,actions)}
function customize(p){let add=[],remove=[];const total=el('strong',{class:'price'},money(p.price));const rows=el('div');
for(const [id,a]of Object.entries(p.additions)){const check=el('input',{type:'checkbox','aria-label':a.name,onchange:()=>{add=check.checked?[...add,id]:add.filter(x=>x!==id);recalc()}});rows.append(el('label',{class:'option'},el('span',{},a.name,el('small',{},' +'+money(a.price))),check))}
if(p.ingredients.length)rows.append(el('h3',{},'Что убрать?'),el('p',{class:'small'},'Снятие ингредиента не меняет цену.'));
for(const name of p.ingredients){const check=el('input',{type:'checkbox','aria-label':'Убрать '+name,onchange:()=>{remove=check.checked?[...remove,name]:remove.filter(x=>x!==name)}});rows.append(el('label',{class:'option'},'Без '+(p.without_labels?.[name]||name),check))}
function recalc(){total.textContent=money(p.price+add.reduce((s,a)=>s+p.additions[a].price,0))}
$('custom-body').replaceChildren(el('div',{class:'row'},el('h2',{},p.name),button('✕',()=>$('custom').close(),'ghost')),el('img',{src:p.image,alt:p.name}),el('p',{},p.description),rows,el('p',{class:'small'},p.allergens),el('div',{class:'row'},total,button('В корзину',()=>{const k=JSON.stringify([p.id,add.sort(),remove.sort()]);const old=cart.find(i=>i.key===k);if(old&&old.qty>=20){toast('До 20 одинаковых порций');return}if(old)old.qty++;else cart.push({key:k,product_id:p.id,qty:1,add:[...add],remove:[...remove],name:p.name,price:p.price+add.reduce((s,a)=>s+p.additions[a].price,0),details:[...add.map(a=>'+'+p.additions[a].name),...remove.map(a=>'без '+(p.without_labels?.[a]||a))].join(', ')});save();nav();$('custom').close();toast('Добавлено в корзину')})));$('custom').showModal()}
function basket(){const root=el('section',{class:'order'},el('div',{class:'bar'},el('h2',{},'Твоя корзина'),el('span',{class:'pill'},'Собрано со вкусом')));if(!cart.length){root.append(el('p',{class:'empty'},'Пока пусто. Самое время выбрать шаурму.'),button('Открыть меню',()=>{tab='menu';render()}));$('app').replaceChildren(root);return}
for(const [index,i]of cart.entries()){root.append(el('article',{class:'card'},el('div',{class:'row'},el('h3',{},i.name),el('b',{},money(i.price*i.qty))),el('p',{},i.details||'Стандартный состав'),el('div',{class:'qty'},button('−',()=>change(-1),'alt'),el('span',{},i.qty),button('+',()=>change(1),'alt'),button('Удалить',()=>{cart.splice(index,1);save();basket();nav()},'ghost'))));function change(d){i.qty=Math.min(20,i.qty+d);if(i.qty<=0)cart.splice(index,1);save();basket();nav()}}
root.append(el('div',{class:'row'},el('h3',{},'Итого'),
  el('strong',{class:'price'},money(cart.reduce((s,i)=>s+i.price*i.qty,0)))));
$('app').replaceChildren(root)}
(async()=>{me=await api('/api/shop/me');await render()})();