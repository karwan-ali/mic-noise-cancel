//! Generates the program icon from the same drawing code the tray uses, and
//! embeds it with the manifest and version information into the .exe.

#[path = "src/icon_draw.rs"]
#[allow(dead_code)]
mod icon_draw;

use std::path::PathBuf;

fn main() {
    println!("cargo:rerun-if-changed=src/icon_draw.rs");
    println!("cargo:rerun-if-changed=res/app.manifest");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }

    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let ico = out.join("app.ico");
    std::fs::write(&ico, icon_draw::ico_file(&[16, 20, 24, 32, 40, 48, 64, 128])).unwrap();
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("res").join("app.manifest");

    let quote = |p: &PathBuf| p.display().to_string().replace('\\', "\\\\");
    let (major, minor, patch) = (
        std::env::var("CARGO_PKG_VERSION_MAJOR").unwrap(),
        std::env::var("CARGO_PKG_VERSION_MINOR").unwrap(),
        std::env::var("CARGO_PKG_VERSION_PATCH").unwrap(),
    );
    let version = std::env::var("CARGO_PKG_VERSION").unwrap();
    let rc = format!(
        r#"#pragma code_page(65001)
1 ICON "{ico}"
1 24 "{manifest}"
1 VERSIONINFO
FILEVERSION {major},{minor},{patch},0
PRODUCTVERSION {major},{minor},{patch},0
FILEOS 0x40004
FILETYPE 0x1
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040904B0"
    BEGIN
      VALUE "FileDescription", "Mic Noise Cancel"
      VALUE "ProductName", "Mic Noise Cancel"
      VALUE "FileVersion", "{version}"
      VALUE "ProductVersion", "{version}"
      VALUE "OriginalFilename", "MicNoiseCancel.exe"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x409, 1200
  END
END
"#,
        ico = quote(&ico),
        manifest = quote(&manifest),
    );
    let rc_path = out.join("app.rc");
    std::fs::write(&rc_path, rc).unwrap();
    embed_resource::compile(&rc_path, embed_resource::NONE).manifest_required().unwrap();
}
