"""Create a persistent private signing key locally. Never commit this directory."""
import argparse,pathlib,secrets,subprocess,os
p=argparse.ArgumentParser();p.add_argument('directory');a=p.parse_args();folder=pathlib.Path(a.directory);folder.mkdir(parents=True,exist_ok=True)
key=folder/'ceh.jks';password=folder/'password.txt'
if key.exists() or password.exists():raise SystemExit('Key already exists; refusing to overwrite')
password.write_text(secrets.token_urlsafe(40));password.chmod(0o600)
subprocess.run(['keytool','-genkeypair','-keystore',str(key),'-storepass:file',str(password),'-keypass:file',str(password),'-alias','ceh','-keyalg','RSA','-keysize','3072','-validity','10000','-dname','CN=Ceh Private Deployment'],check=True);key.chmod(0o600)
print('Private signing files created. Back up both files separately before distributing production APKs.')
