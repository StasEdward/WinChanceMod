# -*- coding: utf-8 -*-
"""
Script for creating a .wotmod (compatible with Python 2.7)
Compiles all .py files from ../src/ to .pyc in ../output/
"""
import zipfile
import os
import py_compile
import shutil

# Configuration
SRC_DIR = "../src"
OUTPUT_DIR = "../output"
GAME_MODS_DIR = "d:\\Games\\World_of_Tanks_EU\\mods\\2.1.0.1"
MOD_NAME = "mod_win_chance.wotmod"
BASE_INTERNAL_PATH = "res/scripts/client/gui/mods"

def ensure_dir(path):
    """Create directory if it doesn't exist"""
    if not os.path.exists(path):
        os.makedirs(path)
        print("Created directory: %s" % path)

def compile_all_py_files():
    """Compile all .py files from src to output directory"""
    
    print("=" * 70)
    print("Step 1: Compiling Python sources to .pyc")
    print("=" * 70)
    
    if not os.path.exists(SRC_DIR):
        print("\nERROR: Source directory %s not found!" % SRC_DIR)
        return False
    
    # Clean output directory
    if os.path.exists(OUTPUT_DIR):
        print("\nCleaning output directory...")
        shutil.rmtree(OUTPUT_DIR)
    
    ensure_dir(OUTPUT_DIR)
    
    compiled_files = []
    errors = []
    
    # Walk through all subdirectories
    for root, dirs, files in os.walk(SRC_DIR):
        for filename in files:
            if not filename.endswith('.py'):
                continue
            
            src_path = os.path.join(root, filename)
            
            # Calculate relative path
            rel_path = os.path.relpath(src_path, SRC_DIR)
            rel_dir = os.path.dirname(rel_path)
            
            # Create corresponding output directory
            out_dir = os.path.join(OUTPUT_DIR, rel_dir) if rel_dir else OUTPUT_DIR
            ensure_dir(out_dir)
            
            # Output .pyc file path
            pyc_filename = filename[:-3] + '.pyc'  # .py -> .pyc
            out_path = os.path.join(out_dir, pyc_filename)
            
            print("\nCompiling: %s" % rel_path)
            
            try:
                py_compile.compile(src_path, cfile=out_path, doraise=True)
                
                # Verify
                if os.path.exists(out_path):
                    size = os.path.getsize(out_path)
                    
                    # Check magic number
                    with open(out_path, 'rb') as f:
                        magic = f.read(4)
                        magic_hex = ''.join(['%02x' % ord(b) for b in magic])
                    
                    if magic_hex.startswith('03f3'):
                        print("  -> %s [%d bytes] [Python 2.7 OK]" % (out_path, size))
                        compiled_files.append((out_path, rel_dir, pyc_filename))
                    else:
                        print("  -> ERROR: Wrong Python version (magic: 0x%s)" % magic_hex)
                        errors.append(rel_path)
                else:
                    print("  -> ERROR: .pyc not created")
                    errors.append(rel_path)
                    
            except Exception as e:
                print("  -> ERROR: %s" % str(e))
                errors.append(rel_path)
    
    print("\n" + "=" * 70)
    print("Compilation Summary:")
    print("  Success: %d files" % len(compiled_files))
    print("  Errors:  %d files" % len(errors))
    print("=" * 70)
    
    if errors:
        print("\nFailed files:")
        for err in errors:
            print("  - %s" % err)
        return False
    
    if not compiled_files:
        print("\nERROR: No .py files found in %s" % SRC_DIR)
        return False
    
    return compiled_files

def build_wotmod(compiled_files):
    """Create .wotmod from compiled .pyc files"""
    
    print("\n" + "=" * 70)
    print("Step 2: Building .wotmod package")
    print("=" * 70)
    
    mod_path = os.path.join(OUTPUT_DIR, MOD_NAME)
    
    print("\nCreating %s..." % mod_path)
    print("Base path in archive: %s\n" % BASE_INTERNAL_PATH)
    
    with zipfile.ZipFile(mod_path, 'w', zipfile.ZIP_STORED) as zipf:
        for out_path, rel_dir, filename in compiled_files:
            # Build internal path for .wotmod
            if rel_dir:
                internal_path = "%s/%s/%s" % (BASE_INTERNAL_PATH, rel_dir.replace('\\', '/'), filename)
            else:
                internal_path = "%s/%s" % (BASE_INTERNAL_PATH, filename)
            
            zipf.write(out_path, internal_path)
            print("  Added: %s" % internal_path)
    
    print("\nCreated: %s" % mod_path)
    
    # Verification
    print("\nVerifying archive structure...")
    with zipfile.ZipFile(mod_path, 'r') as zipf:
        files = zipf.namelist()
        print("  Total files: %d" % len(files))
        for f in files:
            info = zipf.getinfo(f)
            print("    %s (%d bytes)" % (f, info.file_size))
    
    print("\n" + "=" * 70)
    print("MOD READY FOR WOT!")
    print("=" * 70)
    print("\nFile: %s" % mod_path)
    print("\nInstallation:")
    print("  1. Copy to: mods\\<WOT_VERSION>\\")
    print("  2. Launch WoT")
    print("  3. Check python.log for errors")
    print("=" * 70)
    
    # Auto-copy to game mods directory
    try:
        dest_path = os.path.join(GAME_MODS_DIR, MOD_NAME)
        shutil.copy(mod_path, dest_path)
        print("\nAuto-copied to: %s" % dest_path)
    except Exception as e:
        print("\nWarning: Could not copy to game directory: %s" % str(e))
    
    # Delete old python.log
    try:
        log_path = "d:\\Games\World_of_Tanks_EU\\python.log"
        if os.path.exists(log_path):
            os.remove(log_path)
            print("Deleted old python.log")
    except Exception as e:
        print("Warning: Could not delete python.log: %s" % str(e))
    
    return True

if __name__ == "__main__":
    try:
        print("WoT Mod Builder")
        print("Source: %s" % os.path.abspath(SRC_DIR))
        print("Output: %s" % os.path.abspath(OUTPUT_DIR))
        print("")
        
        # Step 1: Compile all .py to .pyc
        compiled_files = compile_all_py_files()
        
        if compiled_files:
            # Step 2: Build .wotmod
            build_wotmod(compiled_files)
        else:
            print("\nCompilation failed. Cannot proceed to build .wotmod")
            
    except Exception as e:
        print("\nERROR: %s" % str(e))
        import traceback
        traceback.print_exc()
    
    raw_input("\nPress Enter to exit...")
