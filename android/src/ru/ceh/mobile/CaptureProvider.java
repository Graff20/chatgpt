package ru.ceh.mobile;
import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.*;
/** Grants the selected camera access to one random capture file, never general storage. */
public class CaptureProvider extends ContentProvider {
 public boolean onCreate(){return true;}
 private File file(Uri uri)throws FileNotFoundException{String path=uri.getPath();if(path==null||!path.matches("/capture/[a-f0-9-]{36}\\.jpg"))throw new FileNotFoundException("Invalid capture URI");File folder=new File(getContext().getCacheDir(),"capture");folder.mkdirs();return new File(folder,uri.getLastPathSegment());}
 public ParcelFileDescriptor openFile(Uri uri,String mode)throws FileNotFoundException{File file=file(uri);int flags=mode.contains("w")?ParcelFileDescriptor.MODE_READ_WRITE|ParcelFileDescriptor.MODE_CREATE|ParcelFileDescriptor.MODE_TRUNCATE:ParcelFileDescriptor.MODE_READ_ONLY;return ParcelFileDescriptor.open(file,flags);}
 public String getType(Uri uri){return "image/jpeg";}
 public Cursor query(Uri uri,String[] projection,String selection,String[] args,String sort){try{File f=file(uri);MatrixCursor c=new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME,OpenableColumns.SIZE});c.addRow(new Object[]{f.getName(),f.length()});return c;}catch(Exception e){return null;}}
 public Uri insert(Uri uri,ContentValues values){throw new UnsupportedOperationException();}
 public int update(Uri uri,ContentValues values,String where,String[] args){throw new UnsupportedOperationException();}
 public int delete(Uri uri,String where,String[] args){return 0;}
}
