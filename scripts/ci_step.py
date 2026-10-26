"""Preserve a build's exit code and expose its diagnostic tail as a CI annotation."""
import subprocess,sys,os
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='backslashreplace')
label=sys.argv[1];command=sys.argv[2:];r=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace');print(r.stdout)
if r.returncode:
 text=r.stdout[-14000:].replace('%','%25').replace('\r','%0D').replace('\n','%0A');print('::error title='+label+'::'+text,flush=True)
sys.exit(r.returncode)
