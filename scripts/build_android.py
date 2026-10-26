"""Build Android client with official SDK tools, no Gradle or third-party dependencies."""
import argparse,os,subprocess,pathlib,zipfile,hashlib,shutil,json,xml.etree.ElementTree as ET
root=pathlib.Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--compiler',help='Optional verified Eclipse ECJ jar when javac is unavailable');p.add_argument('--sdk',default=os.environ.get('ANDROID_HOME',''));p.add_argument('--keystore',required=True);p.add_argument('--password-file',required=True);p.add_argument('--alias',default='ceh');p.add_argument('--out',default=str(root/'downloads'/'ceh-android-pilot.apk'));a=p.parse_args()
sdk=pathlib.Path(a.sdk);tools=sdk/'build-tools'/'35.0.0';jar=sdk/'platforms'/'android-35'/'android.jar';work=root/'build'/'android'
if work.exists():shutil.rmtree(work)
work.mkdir(parents=True,exist_ok=True)
for sub in ['classes','dex']:(work/sub).mkdir(exist_ok=True)
def run(*args):subprocess.run([str(x) for x in args],check=True)
suffix='.bat' if os.name=='nt' else ''
run(tools/'aapt2','compile','--dir',root/'android'/'res','-o',work/'resources.zip')
run(tools/'aapt2','link','-o',work/'base.apk','--manifest',root/'android'/'AndroidManifest.xml','-I',jar,work/'resources.zip')
sources=sorted((root/'android'/'src').rglob('*.java'))
if a.compiler:run('java','-jar',a.compiler,'-encoding','UTF-8','-1.8','-bootclasspath',str(jar)+os.pathsep+str(tools/'core-lambda-stubs.jar'),'-classpath',jar,'-d',work/'classes',*sources)
else:run('javac','-encoding','UTF-8','--release','8','-classpath',jar,'-d',work/'classes',*sources)
run(tools/('d8'+suffix),'--lib',jar,'--min-api','30','--output',work/'dex',*sorted((work/'classes').rglob('*.class')))
shutil.copyfile(work/'base.apk',work/'unaligned.apk')
with zipfile.ZipFile(work/'unaligned.apk','a',zipfile.ZIP_DEFLATED) as z:z.write(work/'dex'/'classes.dex','classes.dex')
run(tools/'zipalign','-f','4',work/'unaligned.apk',work/'unsigned.apk')
out=pathlib.Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
run(tools/('apksigner'+suffix),'sign','--ks',a.keystore,'--ks-key-alias',a.alias,'--ks-pass','file:'+str(pathlib.Path(a.password_file).resolve()),'--v4-signing-enabled','false','--out',out,work/'unsigned.apk')
run(tools/('apksigner'+suffix),'verify','--verbose',out)
digest=hashlib.sha256(out.read_bytes()).hexdigest();out.with_suffix('.apk.sha256').write_text(digest+'  '+out.name+'\n');print('Built',out,'SHA256',digest)

manifest=ET.parse(root/'android'/'AndroidManifest.xml').getroot();ns='{http://schemas.android.com/apk/res/android}';out.with_suffix('.apk.json').write_text(json.dumps({'package':manifest.get('package'),'versionCode':int(manifest.get(ns+'versionCode')),'versionName':manifest.get(ns+'versionName'),'sha256':digest,'source_sha256':hashlib.sha256((root/'android'/'AndroidManifest.xml').read_bytes()+b''.join(p.read_bytes() for p in sources)+b''.join(p.read_bytes() for p in sorted((root/'android'/'res').rglob('*')) if p.is_file())).hexdigest()},indent=2))
