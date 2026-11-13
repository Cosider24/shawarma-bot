"""Один запуск: временный HTTPS-туннель, актуальный адрес в .env, бот и Mini App."""
import atexit
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dotenv import dotenv_values,set_key

ROOT=Path(__file__).resolve().parent
URL=re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')


def tunnel_executable():
    candidates=[ROOT/'cloudflared.exe',shutil.which('cloudflared'),Path(os.getenv('ProgramFiles(x86)','C:/Program Files (x86)'))/'cloudflared/cloudflared.exe']
    for p in candidates:
        if p and Path(p).is_file():return str(p)
    raise RuntimeError('Не найден cloudflared. Установите его или укажите AUTO_TUNNEL=0 и запустите свой HTTPS-туннель.')


def interpreter():
    executable=getattr(sys,'_base_executable',sys.executable) if os.name=='nt' else sys.executable
    env=os.environ.copy();env['PYTHONPATH']=os.pathsep.join(sys.path);env['SHOP_MANAGED']='1'
    return executable,env


def main():
    os.chdir(ROOT)
    runtime=ROOT/'.runtime';runtime.mkdir(exist_ok=True)
    lock=(runtime/'run.lock').open('a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        print('Бот уже запущен. Используйте существующее окно.');return 1
    children=[]
    def stop():
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
    atexit.register(stop)
    try:
        values=dotenv_values(ROOT/'.env');port=int(values.get('WEB_PORT') or '8080')
        executable,env=interpreter()
        if values.get('AUTO_TUNNEL','1')!='0':
            print('Подключаем HTTPS для магазина…',flush=True)
            events=queue.Queue();log=(runtime/'tunnel.log').open('w',encoding='utf-8')
            flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            tunnel=subprocess.Popen([tunnel_executable(),'tunnel','--url',f'http://127.0.0.1:{port}','--protocol','http2','--no-autoupdate'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',creationflags=flags)
            children.append(tunnel)
            def read_output():
                try:
                    for line in tunnel.stdout:
                        log.write(line);log.flush()
                        match=URL.search(line)
                        if match:events.put(match.group(0))
                finally:log.close()
            threading.Thread(target=read_output,daemon=True).start()
            deadline=time.monotonic()+90;url=None
            while time.monotonic()<deadline:
                if tunnel.poll() is not None:raise RuntimeError('Туннель завершился. Причина в .runtime/tunnel.log')
                try:url=events.get(timeout=.5);break
                except queue.Empty:pass
            if not url:raise RuntimeError('Не удалось получить HTTPS-адрес за 90 секунд. Проверьте подключение к интернету и .runtime/tunnel.log')
            set_key(str(ROOT/'.env'),'WEBAPP_URL',url,quote_mode='never')
            env['WEBAPP_URL']=url
            print(f'Магазин: {url}/shop\nКабинет: {url}\nАдрес обновлён автоматически. Оставьте это окно открытым.',flush=True)
        bot=subprocess.Popen([executable,'bot.py'],cwd=ROOT,env=env);children.append(bot)
        while bot.poll() is None:
            if len(children)>1 and children[0].poll() is not None:
                raise RuntimeError('HTTPS-туннель остановился. Перезапустите запускатор для нового адреса.')
            time.sleep(.5)
        return bot.returncode
    except KeyboardInterrupt:return 0
    except Exception as error:
        print(f'Не удалось запустить: {error}',flush=True);return 1
    finally:
        stop();atexit.unregister(stop);lock.close()


if __name__=='__main__':sys.exit(main())
