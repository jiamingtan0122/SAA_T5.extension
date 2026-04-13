# -*- coding: utf-8 -*-
__title__ = "Create Print Set\nFrom Schedule"
__author__ = "JK_Sim"
__doc__ = """Version 1.0
Date: 07.02.2026
_____________________________________________________________________
Description:
Creates a ViewSheetSet (Print Set) from sheets listed in a selected
Drawing List Schedule. User selects schedule, then provides a name
for the print set. If name exists, offers to replace or rename.
_____________________________________________________________________
"""

import clr
from Autodesk.Revit.DB import *
from pyrevit import forms
from System.Collections.Generic import List

# ---------------------------------------------------------
# INITIALIZATION
# ---------------------------------------------------------
doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

# ---------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------

def get_all_schedules():
    """Get all ViewSchedule elements in the document."""
    collector = FilteredElementCollector(doc).OfClass(ViewSchedule)
    schedules = []
    for sched in collector:
        # Filter for Sheet Schedules (Drawing List)
        if sched.Definition.CategoryId == Category.GetCategory(doc, BuiltInCategory.OST_Sheets).Id:
            schedules.append(sched)
    return schedules

def get_sheets_from_schedule(schedule):
    """Extract sheet elements from a schedule."""
    sheets = []
    table = schedule.GetTableData()
    section = table.GetSectionData(SectionType.Body)
    
    # Get the number of rows and columns
    num_rows = section.NumberOfRows
    num_cols = section.NumberOfColumns
    
    # Find the column index for Sheet Number
    sheet_num_col = None
    header_section = table.GetSectionData(SectionType.Header)
    
    for col in range(num_cols):
        try:
            param_id = schedule.Definition.GetField(col).ParameterId
            if param_id == ElementId(BuiltInParameter.SHEET_NUMBER):
                sheet_num_col = col
                break
        except:
            continue
    
    if sheet_num_col is None:
        # Try alternative method - get first column assuming it's sheet number
        sheet_num_col = 0
    
    # Get sheet numbers from schedule (skip header row)
    sheet_numbers = []
    for row in range(1, num_rows):  # Start from 1 to skip header
        try:
            cell_text = schedule.GetCellText(SectionType.Body, row, sheet_num_col)
            if cell_text and cell_text.strip():
                sheet_numbers.append(cell_text.strip())
        except:
            continue
    
    # Find sheets by sheet number
    all_sheets = FilteredElementCollector(doc).OfClass(ViewSheet).ToElements()
    
    for sheet in all_sheets:
        sheet_num = sheet.SheetNumber
        if sheet_num in sheet_numbers:
            sheets.append(sheet)
    
    # Note: ViewSet is an unordered collection that sorts by Element ID,
    # so sorting here won't affect the print order
    
    return sheets

def get_existing_print_sets():
    """Get all existing ViewSheetSets in the document."""
    print_sets = {}
    collector = FilteredElementCollector(doc).OfClass(ViewSheetSet)
    for vs in collector:
        print_sets[vs.Name] = vs
    return print_sets

def generate_unique_name(base_name, existing_names):
    """Generate a unique name by appending (1), (2), etc."""
    if base_name not in existing_names:
        return base_name
    
    counter = 1
    while True:
        new_name = "{0} ({1})".format(base_name, counter)
        if new_name not in existing_names:
            return new_name
        counter += 1

# ---------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------

# Step 1: Get all sheet schedules
schedules = get_all_schedules()

if not schedules:
    forms.alert("No Drawing List Schedules found in the project.", 
                title="No Schedules Found")
    raise Exception("No sheet schedules available.")

# Step 2: Let user select a schedule
schedule_dict = {s.Name: s for s in schedules}
selected_name = forms.SelectFromList.show(
    sorted(schedule_dict.keys()),
    title="Select Drawing List Schedule",
    button_name="Select",
    multiselect=False
)

if not selected_name:
    forms.alert("No schedule selected. Operation cancelled.", 
                title="Cancelled")
    raise Exception("User cancelled schedule selection.")

selected_schedule = schedule_dict[selected_name]

# Step 3: Extract sheets from schedule
try:
    sheets = get_sheets_from_schedule(selected_schedule)
except Exception as e:
    forms.alert("Error reading schedule: {0}".format(str(e)), 
                title="Error")
    raise

if not sheets:
    forms.alert("No sheets found in the selected schedule.", 
                title="No Sheets Found")
    raise Exception("No sheets found in schedule.")

# Step 4: Get existing print sets
existing_sets = get_existing_print_sets()

# Step 5: Ask user for print set name
user_input = forms.ask_for_string(
    default=selected_schedule.Name,
    prompt="Enter name for the Print Set:",
    title="Print Set Name"
)

if not user_input:
    forms.alert("No name provided. Operation cancelled.", 
                title="Cancelled")
    raise Exception("User cancelled print set naming.")

print_set_name = user_input.strip()

# Step 6: Check if name exists and handle accordingly
replace_existing = False
existing_set_to_delete = None

if print_set_name in existing_sets:
    # Ask user to replace or rename
    response = forms.alert(
        "A Print Set named '{0}' already exists.\n\nDo you want to replace it?".format(print_set_name),
        title="Print Set Exists",
        ok=False,
        yes=True,
        no=True
    )
    
    if response:  # User clicked Yes - replace
        replace_existing = True
        existing_set_to_delete = existing_sets[print_set_name]
    else:  # User clicked No - generate unique name
        print_set_name = generate_unique_name(print_set_name, existing_sets.keys())
        forms.alert("Print Set will be created as: '{0}'".format(print_set_name),
                   title="Renamed")

# Step 7: Create or modify the print set

# Now create the print set
with Transaction(doc, "Create Print Set from Schedule") as t:
    t.Start()
    
    try:
        # FIRST: Delete existing print set with same name if it exists
        existing_sets_now = get_existing_print_sets()
        if print_set_name in existing_sets_now:
            doc.Delete(existing_sets_now[print_set_name].Id)
        
        # Get PrintManager and ViewSheetSetting  
        print_manager = doc.PrintManager
        print_manager.PrintRange = PrintRange.Select
        view_sheet_setting = print_manager.ViewSheetSetting
        
        # Create ViewSet with our sheets
        view_set = ViewSet()
        for sheet in sheets:
            view_set.Insert(sheet)
        
        # CRITICAL: Set CurrentViewSheetSet.Views directly
        # This modifies whatever print set CurrentViewSheetSet is pointing to
        # (usually the last created/selected print set)
        view_sheet_setting.CurrentViewSheetSet.Views = view_set
        
        # Try setting IsAutomatic before SaveAs
        try:
            view_sheet_setting.CurrentViewSheetSet.IsAutomatic = True
        except:
            pass
        
        # Save as new print set with our desired name
        view_sheet_setting.SaveAs(print_set_name)
        
        # Set IsAutomatic = True after SaveAs
        try:
            saved_set_collection = FilteredElementCollector(doc).OfClass(ViewSheetSet)
            for vs in saved_set_collection:
                if vs.Name == print_set_name:
                    vs.IsAutomatic = True
                    break
        except:
            pass
        
        t.Commit()
        
        forms.alert(
            "Print Set '{0}' created with {1} sheets.\n\n"
            "Please set the Browser Organization manually in Print dialog.".format(
                print_set_name, len(sheets)),
            title="Success"
        )
        
    except Exception as e:
        t.RollBack()
        forms.alert("Error creating print set: {0}".format(str(e)), 
                   title="Error")
        raise