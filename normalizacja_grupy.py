import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# Obsługujemy typowe rozszerzenia dla JSON i JSON Lines
ALLOWED_EXTENSIONS = {".json", ".jsonl", ".ndjson"}


def json_type(value: Any) -> str:
    """
    Zwraca typ zgodny z nazewnictwem JSON.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _merge_fields(
    target: Dict[str, Dict[str, Any]],
    new: Dict[str, Dict[str, Any]],
) -> None:
    """
    Łączy pola z wielu plików/rekordów w jednej grupie.

    Jeśli ta sama ścieżka występuje wielokrotnie:
      - zbieramy typy,
      - zostawiamy pierwszy sensowny przykład,
      - podmieniamy przykład, jeśli poprzedni był pusty.
    """
    for path, info in new.items():
        if path not in target:
            target[path] = {
                "example": info["example"],
                "types": list(info["types"]),
            }
            continue

        existing = target[path]

        for t in info["types"]:
            if t not in existing["types"]:
                existing["types"].append(t)

        if existing["example"] in (None, [], {}) and info["example"] not in (None, [], {}):
            existing["example"] = info["example"]


def extract_leaf_fields(
    data: Any,
    parent_key: str = "",
    max_list_items: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Wyciąga liście jako słownik:

      ścieżka -> {example, types}

    Dla list sprawdza pierwsze `max_list_items` elementów.
    Jeśli max_list_items=None, sprawdza wszystkie elementy listy.
    """
    fields: Dict[str, Dict[str, Any]] = {}

    if isinstance(data, dict):
        if not data:
            fields[parent_key or "$"] = {
                "example": {},
                "types": ["object"],
            }
            return fields

        for key, value in data.items():
            current_path = f"{parent_key}.{key}" if parent_key else key
            _merge_fields(
                fields,
                extract_leaf_fields(value, current_path, max_list_items),
            )

        return fields

    if isinstance(data, list):
        if not data:
            fields[parent_key or "$"] = {
                "example": [],
                "types": ["array"],
            }
            return fields

        list_path = f"{parent_key}[]" if parent_key else "[]"
        items = data if max_list_items is None else data[:max_list_items]

        for item in items:
            _merge_fields(
                fields,
                extract_leaf_fields(item, list_path, max_list_items),
            )

        return fields

    fields[parent_key or "$"] = {
        "example": data,
        "types": [json_type(data)],
    }

    return fields


def extract_tree_paths(
    data: Any,
    parent_key: str = "",
    max_list_items: Optional[int] = 100,
) -> Set[str]:
    """
    Wyciąga strukturę drzewa jako zbiór ścieżek.

    Przykład:
      {
        "symbol": "BTC",
        "data": [{"price": 1}]
      }

    da m.in.:
      $
      symbol
      data
      data[]
      data[].price
    """
    paths: Set[str] = set()

    if isinstance(data, dict):
        paths.add(parent_key or "$")

        if not data:
            return paths

        for key, value in data.items():
            current_path = f"{parent_key}.{key}" if parent_key else key
            paths.add(current_path)
            paths.update(
                extract_tree_paths(value, current_path, max_list_items)
            )

        return paths

    if isinstance(data, list):
        list_path = f"{parent_key}[]" if parent_key else "[]"
        paths.add(list_path)

        if not data:
            return paths

        items = data if max_list_items is None else data[:max_list_items]

        for item in items:
            paths.update(
                extract_tree_paths(item, list_path, max_list_items)
            )

        return paths

    paths.add(parent_key or "$")
    return paths


def detect_json_format(file_path: Path, sample_lines: int = 50) -> str:
    """
    Wykrywa format pliku:

      - json
      - jsonl
      - jsonl_mixed
      - empty
      - unknown
    """
    parsed = 0
    failed = 0
    nonempty = 0

    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                if nonempty >= sample_lines:
                    break

                nonempty += 1

                try:
                    json.loads(line)
                    parsed += 1
                except json.JSONDecodeError:
                    failed += 1

    except Exception:
        return "unknown"

    if nonempty == 0:
        return "empty"

    # Jeśli wszystkie niepuste linie z próbki są osobnymi JSON-ami,
    # to bardzo prawdopodobne JSON Lines.
    if parsed == nonempty:
        if nonempty == 1:
            # Jedna linia może być zwykłym JSON-em albo jedno-rekordowym JSONL.
            # Traktujemy jako JSON, bo json.load() też to przeczyta.
            return "json"
        return "jsonl"

    # Jeśli linie nie wyglądają na czysty JSONL, spróbuj całość jako jeden JSON.
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            json.load(f)
        return "json"

    except json.JSONDecodeError:
        if parsed > 0:
            return "jsonl_mixed"
        return "unknown"

    except Exception:
        return "unknown"


def extract_from_jsonl(
    file_path: Path,
    max_lines: Optional[int] = 200,
    max_list_items: Optional[int] = 100,
) -> Tuple[Dict[str, Dict[str, Any]], List[str], List[str], Dict[str, int]]:
    """
    Czyta JSON Lines i wyciąga:

      - pola,
      - drzewo struktury,
      - typy rootów,
      - statystyki parsowania.
    """
    fields: Dict[str, Dict[str, Any]] = {}
    tree_paths: Set[str] = set()
    root_types: List[str] = []

    scanned = 0
    parsed = 0
    failed = 0

    with open(file_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            if max_lines is not None and scanned >= max_lines:
                break

            scanned += 1

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                failed += 1
                continue

            parsed += 1
            t = json_type(obj)

            if t not in root_types:
                root_types.append(t)

            _merge_fields(
                fields,
                extract_leaf_fields(obj, "", max_list_items),
            )

            tree_paths.update(
                extract_tree_paths(obj, "", max_list_items)
            )

    stats = {
        "scanned_lines": scanned,
        "parsed_lines": parsed,
        "failed_lines": failed,
    }

    return fields, sorted(tree_paths), root_types, stats


def normalize_root_type(root_type: Any) -> Any:
    """
    Normalizuje root_type tak, żeby dało się porównywać grupy.
    """
    if root_type is None:
        return None

    if isinstance(root_type, list):
        return sorted(root_type)

    return root_type


def make_group_signature(
    parse_as: str,
    root_type: Any,
    tree_paths: List[str],
) -> Tuple[str, ...]:
    """
    Buduje sygnaturę grupy.

    Domyślnie grupa jest tworzona po:
      - sposobie parsowania: json / jsonl,
      - typie roota,
      - drzewie struktury.

    Jeśli chcesz grupować WYŁĄCZNIE po drzewie, możesz użyć:
        return tuple(sorted(tree_paths))
    """
    signature: List[str] = [f"parse_as:{parse_as}"]

    norm_root = normalize_root_type(root_type)
    if norm_root is not None:
        if isinstance(norm_root, list):
            root_repr = ",".join(norm_root)
        else:
            root_repr = str(norm_root)

        signature.append(f"root_type:{root_repr}")

    signature.extend(sorted(tree_paths))
    return tuple(signature)


def make_group_id(signature: Tuple[str, ...]) -> str:
    """
    Krótki identyfikator grupy na podstawie sygnatury.
    """
    payload = "\n".join(signature)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_groups_only(
    directory_path: str,
    output_filename: str = "grupy_do_normalizacji.json",
    max_list_items: Optional[int] = 100,
    max_jsonl_lines: Optional[int] = 200,
) -> Optional[Dict[str, Any]]:
    """
    Buduje tylko listę grup:

      grupa = pliki o tym samym drzewie struktury.

    Wynik nie zawiera osobnej sekcji "files" z opisem każdego pliku.
    """
    dir_path = Path(directory_path)
    output_filepath = dir_path / output_filename

    if not dir_path.is_dir():
        print(f"❌ Katalog nie istnieje: {dir_path}")
        return None

    groups_by_id: Dict[str, Dict[str, Any]] = {}
    unprocessed: List[Dict[str, str]] = []
    scanned_files = 0

    for file_path in sorted(dir_path.iterdir(), key=lambda p: p.name):
        if not file_path.is_file():
            continue

        # Pomijamy plik wynikowy, jeśli już istnieje.
        if file_path.name == output_filename:
            continue

        if file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue

        scanned_files += 1
        detected_format = detect_json_format(file_path)

        if detected_format == "json":
            parse_as = "json"
        elif detected_format in ("jsonl", "jsonl_mixed"):
            parse_as = "jsonl"
        else:
            parse_as = "unknown"

        try:
            if detected_format == "json":
                with open(file_path, "r", encoding="utf-8-sig") as f:
                    content = json.load(f)

                root_type = json_type(content)
                fields = extract_leaf_fields(
                    content,
                    max_list_items=max_list_items,
                )
                tree_paths = sorted(
                    extract_tree_paths(content, max_list_items=max_list_items)
                )

            elif detected_format in ("jsonl", "jsonl_mixed"):
                fields, tree_paths, root_types, stats = extract_from_jsonl(
                    file_path,
                    max_lines=max_jsonl_lines,
                    max_list_items=max_list_items,
                )

                if stats["parsed_lines"] == 0:
                    unprocessed.append({
                        "file": file_path.name,
                        "format": detected_format,
                        "note": "Nie udało się sparsować żadnej linii JSONL.",
                    })
                    continue

                if root_types:
                    root_type = (
                        root_types[0]
                        if len(root_types) == 1
                        else root_types
                    )
                else:
                    root_type = None

            elif detected_format == "empty":
                unprocessed.append({
                    "file": file_path.name,
                    "format": detected_format,
                    "note": "Plik jest pusty.",
                })
                continue

            else:
                unprocessed.append({
                    "file": file_path.name,
                    "format": detected_format,
                    "note": "Nie rozpoznano formatu pliku.",
                })
                continue

        except Exception as e:
            unprocessed.append({
                "file": file_path.name,
                "format": "error",
                "note": str(e),
            })
            continue

        root_type_normalized = normalize_root_type(root_type)

        signature = make_group_signature(
            parse_as,
            root_type_normalized,
            tree_paths,
        )

        group_id = make_group_id(signature)

        if group_id not in groups_by_id:
            groups_by_id[group_id] = {
                "group_id": group_id,
                "parse_as": parse_as,
                "root_type": root_type_normalized,
                "formats": [],
                "file_count": 0,
                "files": [],
                "tree_paths": tree_paths,
                "fields": {},
            }

        group = groups_by_id[group_id]

        group["files"].append(file_path.name)
        group["file_count"] = len(group["files"])

        if detected_format not in group["formats"]:
            group["formats"].append(detected_format)

        # Scal pola z całego pliku/rekordów z dotychczasowymi polami grupy.
        _merge_fields(group["fields"], fields)

        print(
            f"✅ {file_path.name} -> {detected_format}, "
            f"group_id: {group_id}"
        )

    # Sortowanie grup: największe najpierw.
    groups = sorted(
        groups_by_id.values(),
        key=lambda g: (g["file_count"], g["group_id"]),
        reverse=True,
    )

    # Porządki końcowe: dodajemy ścieżki liści i sortujemy formaty.
    final_groups = []

    for g in groups:
        final_groups.append({
            "group_id": g["group_id"],
            "parse_as": g["parse_as"],
            "root_type": g["root_type"],
            "formats": sorted(g["formats"]),
            "file_count": g["file_count"],
            "files": sorted(g["files"]),
            "paths": sorted(g["fields"].keys()),
            "tree_paths": g["tree_paths"],
            "fields": g["fields"],
        })

    result = {
        "directory": str(dir_path.resolve()),
        "summary": {
            "total_scanned_files": scanned_files,
            "grouped_files": sum(g["file_count"] for g in final_groups),
            "unique_groups": len(final_groups),
            "unprocessed_count": len(unprocessed),
            "unprocessed": unprocessed,
        },
        "groups": final_groups,
    }

    with open(output_filepath, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print(f"ZNALEZIONO {len(final_groups)} GRUP STRUKTUR")
    print("=" * 60 + "\n")

    for idx, group in enumerate(final_groups, start=1):
        print(
            f"Grupa {idx}: "
            f"pliki={group['file_count']}, "
            f"parse_as={group['parse_as']}, "
            f"group_id={group['group_id']}"
        )

        for filename in group["files"]:
            print(f"  - {filename}")

        print("-" * 60)

    if unprocessed:
        print("\n⚠️ Pliki nieprzetworzone:")
        for item in unprocessed:
            print(f"  - {item['file']} | {item['format']} | {item['note']}")

    print(f"\n🚀 Gotowe. Zapisano grupy do: {output_filepath}")

    return result


if __name__ == "__main__":
    DIRECTORY = "/home/radek_debian/projects/gielda_dane/pobrane_dane_v4"

    build_groups_only(
        directory_path=DIRECTORY,
        output_filename="grupy_do_normalizacji.json",
        max_list_items=100,
        max_jsonl_lines=200,
    )