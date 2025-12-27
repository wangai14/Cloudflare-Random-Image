import json
import math
import shutil
import urllib.parse
import sys
import os
from pathlib import Path
from itertools import cycle

# Try to import Pillow for image dimension detection and conversion
try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("Error: Pillow is required for WebP conversion. Please install it with 'pip install Pillow'.")
    sys.exit(1)

# ================= 配置区域 =================

# 基础 Hex 长度 (保底)
# 1 -> 16 files, 2 -> 256 files, 3 -> 4096 files
MIN_HEX_LEN = 1

# 图片源目录 (本地)
SOURCE_DIR = Path("image")

# 输出目录 (本地)
OUTPUT_DIR = Path("dist")

# 输出文件后缀
FILE_EXT = ".webp"

# ===========================================

def calculate_hex_len(item_count: int, min_len: int) -> int:
    """根据数据量自动计算所需的 Hex 长度"""
    if item_count == 0:
        return min_len
    # 计算需要的位数: log16(count)
    needed = math.ceil(math.log(item_count, 16))
    return max(min_len, needed)

def generate_cf_rule(hex_len: int) -> str:
    """生成 Cloudflare 规则表达式"""
    
    # 1. Landscape (横屏)
    # Match: (http.request.uri.query contains "c=l")
    rule_landscape = f'concat("/categories/l/", substring(uuidv4(cf.random_seed), 0, {hex_len}), "{FILE_EXT}")'
    
    # 2. Portrait (竖屏)
    # Match: (http.request.uri.query contains "c=p")
    rule_portrait = f'concat("/categories/p/", substring(uuidv4(cf.random_seed), 0, {hex_len}), "{FILE_EXT}")'
    
    # 3. All (全随机)
    # Match: (not http.request.uri.query contains "c=")
    rule_all = f'concat("/all/", substring(uuidv4(cf.random_seed), 0, {hex_len}), "{FILE_EXT}")'
    
    content = [
        "===========================================================",
        "请在 Cloudflare -> Rules -> Transform Rules 中创建以下 3 条规则",
        "建议顺序: 1. Landscape, 2. Portrait, 3. Random (All)",
        "===========================================================",
        "",
        "--- Rule 1: Landscape (横屏) ---",
        "Rule Name: Random Image - Landscape",
        'When incoming requests match: (http.request.uri.query contains "c=l")',
        "Path Rewrite: Dynamic",
        f"Expression: {rule_landscape}",
        "",
        "--- Rule 2: Portrait (竖屏) ---",
        "Rule Name: Random Image - Portrait",
        'When incoming requests match: (http.request.uri.query contains "c=p")',
        "Path Rewrite: Dynamic",
        f"Expression: {rule_portrait}",
        "",
        "--- Rule 3: Random (全随机) ---",
        "Rule Name: Random Image - All",
        'When incoming requests match: (not http.request.uri.query contains "c=")',
        "Path Rewrite: Dynamic",
        f"Expression: {rule_all}",
        ""
    ]
    
    return "\n".join(content)

def ensure_dir(path: Path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)

def scan_images(source_dir: Path):
    """扫描所有图片并分类"""
    all_imgs = []
    landscape_imgs = []
    portrait_imgs = []
    
    if not source_dir.exists():
        print(f"Error: Source directory '{source_dir}' does not exist.")
        return [], [], []

    # 支持的图片扩展名
    exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff'}
    
    print(f"Scanning images in {source_dir}...")
    
    for file_path in source_dir.rglob('*'):
        if file_path.is_file() and file_path.suffix.lower() in exts:
            
            item = {'path': file_path}
            
            # 分类
            try:
                with Image.open(file_path) as img:
                    width, height = img.size
                    if width > height:
                        landscape_imgs.append(item)
                    else:
                        portrait_imgs.append(item)
            except Exception as e:
                print(f"Warning: Could not open {file_path}: {e}")
                # 无法识别的图片默认归为 all
            
            all_imgs.append(item)

    return all_imgs, landscape_imgs, portrait_imgs

def convert_and_save(source_path: Path, target_path: Path):
    """将图片转换为 WebP 并保存"""
    try:
        with Image.open(source_path) as img:
            # 转换为 RGB 模式（防止 PNG 透明通道在不支持的模式下报错，WebP 支持 RGBA 但保险起见）
            if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                 # 保持透明度
                 pass 
            else:
                 # 确保非透明图是 RGB
                 img = img.convert('RGB')
            
            img.save(target_path, 'WEBP', quality=85)
    except Exception as e:
        print(f"Error converting {source_path}: {e}")

def create_symlink_or_copy(target: Path, link_name: Path):
    """创建软链接，如果失败（如Windows无权限）则复制"""
    # 目标相对于链接位置的路径
    try:
        # 计算相对路径：从 link_name 的父目录 指向 target
        # target 是 dist/all/xx.webp
        # link_name 是 dist/categories/l/xx.webp
        # 相对路径应该是 ../../all/xx.webp
        relative_target = os.path.relpath(target, link_name.parent)
        os.symlink(relative_target, link_name)
    except (OSError, AttributeError):
        # Windows 需要管理员权限才能 symlink，或者文件系统不支持
        # 退化为复制
        shutil.copy2(target, link_name)

def write_files(data_list, output_subdir: Path, hex_len: int, is_symlink_source=False, source_map=None):
    """
    将数据分片写入文件 (Fill-Full 策略)
    is_symlink_source: 如果为 True，说明是写入 dist/all/，需要转换图片并保存
    source_map: 如果不为 None，说明是写入分类目录，需要查找对应的 dist/all/ 下的文件路径进行软链
    """
    if not output_subdir.exists():
        output_subdir.mkdir(parents=True)

    if not data_list:
        print(f"  [Warning] No data for {output_subdir.name}")
        return {}

    total_slots = 16 ** hex_len
    buckets = [[] for _ in range(total_slots)]
    
    # 循环分发
    data_cycle = cycle(data_list)
    for i in range(total_slots):
        buckets[i] = next(data_cycle)
    
    generated_map = {} # source_path -> path_in_all (Str)

    for i in range(total_slots):
        hex_name = f"{i:0{hex_len}x}"
        target_filename = f"{hex_name}{FILE_EXT}"
        target_path = output_subdir / target_filename
        
        source_item = buckets[i]
        source_path = source_item['path']
        
        if is_symlink_source:
            # 实体写入 (dist/all)
            convert_and_save(source_path, target_path)
            # 记录映射 (只记录第一次出现的即可，或者覆盖也无所谓，只要指向的内容是对的)
            generated_map[source_path] = target_path
        else:
            # 软链写入 (dist/categories/...)
            # 找到这张图在 dist/all 中的位置
            if source_map and source_path in source_map:
                real_target = source_map[source_path]
                create_symlink_or_copy(real_target, target_path)
            else:
                # 理论上不应该发生，除非 l/p 里的图不在 all 里
                print(f"Warning: {source_path} not found in all map")
                # 降级：直接转存
                convert_and_save(source_path, target_path)

    print(f"  Generated {total_slots} files in {output_subdir}")
    return generated_map

def main():
    # 1. 扫描
    all_imgs, landscape, portrait = scan_images(SOURCE_DIR)
    
    print(f"Found {len(all_imgs)} images.")
    print(f"  Landscape: {len(landscape)}")
    print(f"  Portrait:  {len(portrait)}")
    
    if len(all_imgs) == 0:
        print("Error: No images found in source directory. Please check if 'image' folder exists and contains images.")
        sys.exit(1)

    # 2. 计算 Hex 长度
    hex_len = calculate_hex_len(len(all_imgs), MIN_HEX_LEN)
    print(f"Calculated Hex Length: {hex_len} (Max capacity: {16**hex_len})")
    
    # 3. 清理并生成目录
    ensure_dir(OUTPUT_DIR)
    
    # 4. 写入文件 (WebP)
    
    # 4.1 生成 all (实体文件)
    print("Generating 'all' directory (Entity files)...")
    all_map = write_files(all_imgs, OUTPUT_DIR / "all", hex_len, is_symlink_source=True)
    
    # 4.2 生成 categories (软链接)
    print("Generating 'categories/l' directory (Symlinks)...")
    write_files(landscape, OUTPUT_DIR / "categories" / "l", hex_len, is_symlink_source=False, source_map=all_map)
    
    print("Generating 'categories/p' directory (Symlinks)...")
    write_files(portrait, OUTPUT_DIR / "categories" / "p", hex_len, is_symlink_source=False, source_map=all_map)
    
    # 5. 生成 rules.txt
    rules = generate_cf_rule(hex_len)
    with open(OUTPUT_DIR / "rules.txt", 'w', encoding='utf-8') as f:
        f.write(rules)
        
    print("Done! Check 'dist' directory.")

if __name__ == "__main__":
    main()
