# -*- coding: utf-8 -*-
__title__ = "Transfer\nSingle"
__author__ = "JK_Sim"
__doc__ = """Version = 1.18
Date    = 04.11.2025
_____________________________________________________________________
Description:

Transfers selected loadable family element(s) to another open project.
Preserves original family names (no suffix renames).

Single transaction: "TransferSingle"
_____________________________________________________________________
"""

from pyrevit import revit, DB
from pyrevit.forms import SelectFromList
from Autodesk.Revit.DB import (
    ElementTransformUtils, CopyPasteOptions,
    IDuplicateTypeNamesHandler, DuplicateTypeAction,
    Transform, Transaction, FilteredElementCollector,
    Family, FamilySymbol, FamilyInstance,
    IFailuresPreprocessor, FailureProcessingResult
)
from System.Collections.Generic import List
import sys
import re

# ---------------- Duplicate handler ----------------
class DuplicateHandler(IDuplicateTypeNamesHandler):
    def OnDuplicateTypeNamesFound(self, args):
        return DuplicateTypeAction.UseDestinationTypes

# ---------------- Failure preprocessor (auto-OK all warnings) ----------------
class AutoOKAllWarnings(DB.IFailuresPreprocessor):
    def PreprocessFailures(self, fa):
        try:
            for fm in fa.GetFailureMessages():
                if fm.GetSeverity() == DB.FailureSeverity.Warning:
                    fa.DeleteWarning(fm)
        except:
            pass
        return FailureProcessingResult.Continue

# ---------------- Helpers (IronPython-safe) ----------------
def safe_symbol_name(symbol):
    try:
        p = symbol.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM)
        if p:
            return p.AsString()
    except:
        pass
    try:
        return symbol.Name
    except:
        return "<Unknown>"

def safe_family_name(family):
    try:
        return family.Name
    except:
        try:
            p = family.get_Parameter(DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME)
            if p:
                return p.AsString()
        except:
            pass
    return "<Unknown>"

def find_family_by_name(rvt_doc, family_name):
    for fam in FilteredElementCollector(rvt_doc).OfClass(Family):
        if safe_family_name(fam) == family_name:
            return fam
    return None

def find_symbol_by_family_and_type(rvt_doc, family_name, type_name):
    for sym in FilteredElementCollector(rvt_doc).OfClass(FamilySymbol).WhereElementIsElementType():
        fam = getattr(sym, "Family", None)
        if fam and safe_family_name(fam) == family_name and safe_symbol_name(sym) == type_name:
            return sym
    return None

def read_type_parameters(symbol):
    data = {}
    for p in symbol.Parameters:
        try:
            if p.IsReadOnly:
                continue
            pname = p.Definition.Name
            st = p.StorageType
            if st == DB.StorageType.Double:
                data[pname] = ("double", p.AsDouble())
            elif st == DB.StorageType.Integer:
                data[pname] = ("int", p.AsInteger())
            elif st == DB.StorageType.String:
                data[pname] = ("str", p.AsString())
        except:
            pass
    return data

def apply_type_parameters(target_symbol, param_map):
    for pname, (ptype, val) in param_map.items():
        try:
            tp = target_symbol.LookupParameter(pname)
            if tp and not tp.IsReadOnly:
                st = tp.StorageType
                if ptype == "double" and st == DB.StorageType.Double:
                    tp.Set(val)
                elif ptype == "int" and st == DB.StorageType.Integer:
                    tp.Set(val)
                elif ptype == "str" and st == DB.StorageType.String:
                    if val is not None:
                        tp.Set(val)
        except:
            pass

def derive_base_if_renamed(name, before_fam_names, source_fam_names):
    """
    Return (base_name, is_renamed) where is_renamed True only if 'name' equals
    <base> + optional space + digits, and <base> exists in before_fam_names or in source_fam_names.
    Handles 'Family 1' and 'Family1'.
    """
    candidates = set(before_fam_names)
    candidates.update(source_fam_names)
    for base in candidates:
        if name == base:
            return (name, False)
        if name.startswith(base):
            rest = name[len(base):]
            # match "1", " 1", " 23" etc.
            if re.match(r"^\s*\d+$", rest):
                return (base, True)
    return (name, False)

# ---------------- MAIN ----------------
doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
app = doc.Application

open_docs = list(app.Documents)
choices = [d.Title for d in open_docs if d.Title != doc.Title and not d.IsLinked and not d.IsFamilyDocument]
if not choices:
    sys.exit()

target_doc_name = SelectFromList.show(choices, title="Select Target Project", button_name="Transfer")
if not target_doc_name:
    sys.exit()

target_doc = None
for d in open_docs:
    if d.Title == target_doc_name:
        target_doc = d
        break

sel_ids = list(uidoc.Selection.GetElementIds())
if not sel_ids:
    sys.exit()

source_elems = [doc.GetElement(i) for i in sel_ids]

# Collect source family/type data
wanted = {}
source_fam_names = set()
for e in source_elems:
    try:
        sym = doc.GetElement(e.GetTypeId())
        fam = getattr(sym, "Family", None)
        if fam is None or not isinstance(fam, Family):
            sys.exit()
        fam_name = safe_family_name(fam)
        type_name = safe_symbol_name(sym)
        source_fam_names.add(fam_name)
        if (fam_name, type_name) not in wanted:
            wanted[(fam_name, type_name)] = read_type_parameters(sym)
    except:
        sys.exit()

# ---------------- Single Transaction: TransferSingle ----------------
t = DB.Transaction(target_doc, "TransferSingle")
t.Start()

# Auto-OK all warnings
fh = t.GetFailureHandlingOptions()
fh.SetFailuresPreprocessor(AutoOKAllWarnings())
t.SetFailureHandlingOptions(fh)

try:
    # Snapshot of existing families (by name) before copy
    before_fams = {safe_family_name(fam): fam for fam in FilteredElementCollector(target_doc).OfClass(Family)}
    before_fam_names = set(before_fams.keys())

    # Step 1: ensure required types exist (and sync parameters) in existing families
    for (fam_name, type_name), param_map in wanted.items():
        tgt_family = before_fams.get(fam_name)
        if tgt_family:
            existing = find_symbol_by_family_and_type(target_doc, fam_name, type_name)
            if existing:
                apply_type_parameters(existing, param_map)
            else:
                base_symbol = None
                for sid in tgt_family.GetFamilySymbolIds():
                    base_symbol = target_doc.GetElement(sid)
                    break
                if base_symbol:
                    new_symbol = base_symbol.Duplicate(type_name)
                    apply_type_parameters(new_symbol, param_map)

    # Step 2: copy elements using destination types
    options = CopyPasteOptions()
    options.SetDuplicateTypeNamesHandler(DuplicateHandler())
    copied_ids = ElementTransformUtils.CopyElements(
        doc,
        List[DB.ElementId](sel_ids),
        target_doc,
        Transform.Identity,
        options
    )

    # Step 3: detect families added by the paste
    after_fams = {safe_family_name(fam): fam for fam in FilteredElementCollector(target_doc).OfClass(Family)}
    new_fams = [fam for name, fam in after_fams.items() if name not in before_fams]

    # Step 4: for each new family, treat as renamed duplicate only if it is <base><idx>
    for copied_family in new_fams:
        copied_name = safe_family_name(copied_family)
        base_name, is_renamed = derive_base_if_renamed(copied_name, before_fam_names, source_fam_names)
        if not is_renamed:
            continue  # it's a genuinely new family; ignore

        original_family = before_fams.get(base_name)
        if not original_family:
            continue  # base didn't exist before copy; ignore

        # Build map of original family's symbols by type name
        original_symbols = {}
        for sid in original_family.GetFamilySymbolIds():
            s = target_doc.GetElement(sid)
            original_symbols[safe_symbol_name(s)] = s

        # Reassign only newly-copied instances that belong to the copied family
        for new_id in copied_ids:
            inst = target_doc.GetElement(new_id)
            if not isinstance(inst, FamilyInstance):
                continue
            sym = inst.Symbol
            fam_inst = getattr(sym, "Family", None)
            if not fam_inst or fam_inst.Id != copied_family.Id:
                continue
            tname = safe_symbol_name(sym)
            if tname in original_symbols:
                inst.ChangeTypeId(original_symbols[tname].Id)

        # Remove the renamed duplicate family
        try:
            target_doc.Delete(copied_family.Id)
        except:
            pass

    t.Commit()

except:
    if t.HasStarted():
        t.RollBack()
