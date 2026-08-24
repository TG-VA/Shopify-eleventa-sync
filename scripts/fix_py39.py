#!/usr/bin/env python3
"""
Script para hacer el código compatible con Python 3.9.
Agrega 'from __future__ import annotations' al inicio de cada archivo .py
en middleware/ y shared/. Esto habilita la sintaxis moderna de tipos
(X | None, dict[str, Any], list[str]) en Python 3.9.
"""
import os
import glob

DIRS = ["middleware", "shared"]
IMPORT_LINE = "from __future__ import annotations\n"

count = 0
for d in DIRS:
    for filepath in glob.glob(f"{d}/**/*.py", recursive=True):
        with open(filepath, "r") as f:
            content = f.read()
        
        # Saltar si ya tiene el import o si está vacío
        if IMPORT_LINE.strip() in content:
            continue
        if not content.strip():
            continue
            
        # Insertar después del docstring del módulo si existe, o al inicio
        lines = content.split("\n")
        insert_idx = 0
        
        # Si empieza con docstring, insertar después
        if lines[0].strip().startswith('"""') or lines[0].strip().startswith("'''"):
            quote = '"""' if '"""' in lines[0] else "'''"
            if lines[0].strip().endswith(quote) and len(lines[0].strip()) > 3:
                insert_idx = 1
            else:
                for i, line in enumerate(lines[1:], 1):
                    if quote in line:
                        insert_idx = i + 1
                        break
        
        # Insertar el import
        lines.insert(insert_idx, "")
        lines.insert(insert_idx + 1, IMPORT_LINE.strip())
        
        with open(filepath, "w") as f:
            f.write("\n".join(lines))
        
        count += 1
        print(f"  ✅ {filepath}")

print(f"\n🎉 {count} archivos actualizados para Python 3.9")
