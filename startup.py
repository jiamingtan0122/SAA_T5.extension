# -*- coding: utf-8 -*-
import os, time, traceback

# ---- settings ----
LOG_ENABLED = False           # troubleshooting log; set False after testing
# a fixed per-user folder (pyRevit gives each session its own TEMP folder)
LOG = os.path.join(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP", "."),
                   "vt_guard", "vt_guard_log.txt")
REFRESH_SEC = 10             # raise to 30 if big models feel slow when idle
# Which protections are switched on is set in the Rules Editor (Features
# button) and stored in vt_rules.json. The defaults are in lib\vt_lock.py.
OVERRIDE_SCAN_LIMIT = 40000  # views with more elements than this are not scanned for element overrides
# (audit settings are at the top of lib\vt_lock.py)
# ------------------


def log(msg):
    if not LOG_ENABLED:
        return
    try:
        folder = os.path.dirname(LOG)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(LOG, "a") as f:
            f.write("{} {}\n".format(time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def log_exc(label):
    log("ERROR " + label + ": " + repr(traceback.format_exc()))


log("startup.py begin (v25)")
try:
    log("loaded from: {}".format(__file__))
except Exception:
    pass

try:
    from System import Guid
    log("import System OK")
except Exception:
    log_exc("import System")
    raise

try:
    from Autodesk.Revit.DB import (IUpdater, UpdaterId, UpdaterRegistry,
                                   ElementClassFilter, ElementCategoryFilter,
                                   BuiltInCategory, Element, View,
                                   ChangePriority, BuiltInParameter, ElementId,
                                   FilteredElementCollector,
                                   OverrideGraphicSettings, RevitLinkInstance,
                                   RevitLinkType)
    log("import DB core OK")
except Exception:
    log_exc("import DB core")
    raise

try:
    from pyrevit import HOST_APP
    log("import pyrevit OK")
except Exception:
    log_exc("import pyrevit")
    raise

try:
    import vt_lock
    log("import vt_lock OK")
except Exception:
    log_exc("import vt_lock")
    raise

HAVE_FAIL = False
try:
    from Autodesk.Revit.DB import (FailureDefinition, FailureDefinitionId,
                                   FailureDefinitionRegistry, FailureSeverity,
                                   FailureMessage, FailureProcessingResult)
    from Autodesk.Revit.UI import TaskDialog
    HAVE_FAIL = True
    log("import failure types OK")
except Exception:
    log_exc("import failure types (guard will use raise)")

FAIL_ID = None
if HAVE_FAIL:
    try:
        FAIL_ID = FailureDefinitionId(
            Guid("0b7d3c52-8e1a-4f6b-a2c9-5d41e7b93f18"))
        FailureDefinition.CreateFailureDefinition(
            FAIL_ID, FailureSeverity.Error,
            "A protected view template was changed. "
            "Ask a BIM admin to unlock it.")
        log("failure definition OK")
    except Exception:
        # Revit only lets definitions be created at application startup. After
        # a pyRevit Reload the one made earlier in this session is still
        # registered, so reuse it instead of giving up on the clean dialog.
        try:
            if FailureDefinitionRegistry.FindFailureDefinition(FAIL_ID) is not None:
                log("failure definition already registered - reusing it")
            else:
                log_exc("failure definition (not found, using raise)")
                FAIL_ID = None
        except Exception:
            log_exc("failure definition (lookup failed, using raise)")
            FAIL_ID = None

_snap = {}      # doc title -> {protected template uniqueid: current name}
_orig = {}      # doc title -> {template uniqueid: name it was protected under}
_ids = {}       # doc title -> {protected template element id (int): name}
_vt = {}        # doc title -> {view uniqueid: protected template uniqueid}
_known = {}     # doc title -> {uniqueid of every view that existed at the last refresh}
_allt = {}      # doc title -> {template uniqueid: name}  (all templates)
_allids = {}    # doc title -> {template element id (int): name}  (all templates)
_last = {}      # doc title -> last refresh time
_pending = {"msg": None}


def refresh(doc):
    title = doc.Title
    first_time = title not in _snap
    snap = _snap.setdefault(title, {})
    orig = _orig.setdefault(title, {})
    before = len(snap)
    names = vt_lock.protected_names(doc)
    views = list(FilteredElementCollector(doc).OfClass(View))

    # 0. stop protecting templates whose original name left the list
    for uid in list(snap.keys()):
        if not vt_lock.name_matches(orig.get(uid), names):
            snap.pop(uid, None)
            orig.pop(uid, None)

    # 1. protected templates, by current name (remembered by UniqueId)
    allt = {}
    allids = {}
    for v in views:
        if getattr(v, "IsTemplate", False):
            allt[v.UniqueId] = v.Name
            allids[v.Id.IntegerValue] = v.Name
            if vt_lock.name_matches(v.Name, names):
                snap[v.UniqueId] = v.Name
                orig.setdefault(v.UniqueId, v.Name)
    _allt[title] = allt
    _allids[title] = allids

    # 2. prune deleted templates, rebuild the element-id map
    ids = {}
    for uid in list(snap.keys()):
        e = doc.GetElement(uid)
        if e is None:
            snap.pop(uid, None)
            orig.pop(uid, None)
        else:
            snap[uid] = e.Name
            ids[e.Id.IntegerValue] = e.Name
    _ids[title] = ids

    # 3. which views currently use a protected template
    vt = {}
    known = set()
    for v in views:
        if getattr(v, "IsTemplate", True):
            continue
        known.add(v.UniqueId)
        tid = v.ViewTemplateId
        if tid == ElementId.InvalidElementId:
            continue
        t = doc.GetElement(tid)
        if t is not None and t.UniqueId in snap:
            vt[v.UniqueId] = t.UniqueId
    _vt[title] = vt
    _known[title] = known

    _last[title] = time.time()
    if first_time:
        try:
            log("model {}: matched keys={} protected patterns={} "
                "templates in model={} now protected={}".format(
                    title, [k for k, v in vt_lock.matches(doc)],
                    sorted(names), len(allt), len(snap)))
        except Exception:
            log_exc("model summary")
    if len(snap) != before:
        log("snapshot {}: {} protected, ids={}".format(
            title, len(snap), sorted(ids.keys())))
    return snap


def get_snap(doc):
    if doc.Title not in _snap:
        refresh(doc)
    return _snap[doc.Title]


def get_vt(doc):
    if doc.Title not in _vt:
        refresh(doc)
    return _vt[doc.Title]


def is_protected(doc, t):
    snap = get_snap(doc)
    if t.UniqueId in snap:
        return True
    if vt_lock.name_matches(t.Name, vt_lock.protected_names(doc)):
        snap[t.UniqueId] = t.Name      # remember it so a rename is caught later
        _orig.setdefault(doc.Title, {})[t.UniqueId] = t.Name
        _ids.setdefault(doc.Title, {})[t.Id.IntegerValue] = t.Name
        return True
    return False


# While Revit synchronises with central or reloads the latest changes, the
# changes arriving are other people's work, already checked when they made
# them. Blocking them would stop the sync ("Error - cannot be ignored").
_syncing = {"on": False, "since": 0.0, "what": ""}


def sync_active():
    return _syncing["on"]


def sync_begin(what):
    _syncing.update({"on": True, "since": time.time(), "what": what})
    log("{} started: guard paused until it finishes".format(what))


def sync_end(doc, what):
    _syncing["on"] = False
    log("{} finished: guard active again ({:.0f} s)".format(
        what, time.time() - _syncing["since"]))
    try:
        title = doc.Title
        _ov.pop(title, None)          # overrides may have legitimately changed
        for d in (_snap, _orig, _ids, _vt, _known, _allt, _allids):
            d.pop(title, None)
        refresh(doc)                  # re-learn templates and their views
    except Exception:
        log_exc("refresh after " + what)


_real_audit = vt_lock.audit


def _audit(*a, **k):
    """No audit rows while syncing: those changes are other people's."""
    if _syncing["on"]:
        return
    return _real_audit(*a, **k)


vt_lock.audit = _audit


def block(doc, eid, text, event, template, details=""):
    if _syncing["on"]:
        log("NOT blocked (sync in progress): " + text)
        return
    vt_lock.audit(doc, event, template, details)
    if FAIL_ID is None:
        log("blocking (raise): " + text)
        raise Exception(text)
    log("blocking: " + text)
    try:
        _pending["msg"] = text
        msg = FailureMessage(FAIL_ID)
        if eid is not None:
            msg.SetFailingElement(eid)
        doc.PostFailure(msg)
    except Exception:
        log_exc("PostFailure failed, raising instead")
        raise Exception(text)


def any_live_element(doc):
    """An element that still exists, to hang a failure on during a delete."""
    try:
        av = doc.ActiveView
        if av is not None:
            return av.Id
    except Exception:
        pass
    try:
        for v in FilteredElementCollector(doc).OfClass(View):
            if not getattr(v, "IsTemplate", True):
                return v.Id
    except Exception:
        pass
    return None


def clear_template_of_new_view(doc, v):
    """Feature 'duplicate_clear_template': a new view that came with a
    protected template (a duplicate) is set to no template. True if cleared."""
    try:
        if not vt_lock.feature("duplicate_clear_template"):
            return False
        t = protected_template_of(doc, v)
        if t is None:
            return False
        v.ViewTemplateId = ElementId.InvalidElementId
        vt_lock.audit(doc, "new-view-template-cleared", t.Name,
                      u"view: " + v.Name)
        log("new view '{}': template '{}' set to none".format(v.Name, t.Name))
        return True
    except Exception:
        log_exc("clear_template_of_new_view")
        return False


class VTGuard(IUpdater):
    """Blocks edits to protected view templates (by UniqueId or name) and
    audits template edits, renames and creations."""

    def __init__(self, addin_id):
        self.uid = UpdaterId(addin_id,
                             Guid("6f1c2b7e-3a54-4c1d-9b0e-2d8f5a7c1e90"))

    def Execute(self, data):
        if _syncing["on"]:
            return
        doc = data.GetDocument()
        unlocked = vt_lock.is_unlocked(doc)
        allt = _allt.setdefault(doc.Title, {})

        for eid in data.GetAddedElementIds():
            v = doc.GetElement(eid)
            if v is not None and getattr(v, "IsTemplate", False):
                if vt_lock.AUDIT_ALL_TEMPLATES:
                    vt_lock.audit(doc, "create", v.Name, "")
                allt[v.UniqueId] = v.Name
            elif v is not None and isinstance(v, View):
                clear_template_of_new_view(doc, v)

        for eid in data.GetModifiedElementIds():
            v = doc.GetElement(eid)
            if v is None or not getattr(v, "IsTemplate", False):
                continue
            prev = allt.get(v.UniqueId)
            renamed = prev is not None and prev != v.Name
            action = "rename" if renamed else "edit"
            details = u"{} -> {}".format(prev, v.Name) if renamed else ""
            prot = is_protected(doc, v)
            if prot and not unlocked and vt_lock.feature("template_edit"):
                block(doc, eid, "View template '{}' is protected. "
                      "Ask a BIM admin to unlock it.".format(prev or v.Name),
                      "blocked-" + action, prev or v.Name, details)
                return
            if prot or vt_lock.AUDIT_ALL_TEMPLATES:
                vt_lock.audit(doc, action, v.Name, details)
            allt[v.UniqueId] = v.Name

        if unlocked and time.time() - _last.get(doc.Title, 0) > 30:
            refresh(doc)

    def GetUpdaterId(self):
        return self.uid

    def GetUpdaterName(self):
        return "VT Guard"

    def GetAdditionalInformation(self):
        return "Protects view templates"

    def GetChangePriority(self):
        return ChangePriority.Views


class VTAssignGuard(IUpdater):
    """Blocks assigning a protected template to a view, and switching a
    view away from a protected template. Audits assignments."""

    def __init__(self, addin_id):
        self.uid = UpdaterId(addin_id,
                             Guid("a3d9e6f1-52b8-4c07-8f3a-71c2b04d9e5b"))

    def Execute(self, data):
        if _syncing["on"]:
            return
        doc = data.GetDocument()
        unlocked = vt_lock.is_unlocked(doc)
        vt = get_vt(doc)
        snap = get_snap(doc)
        for eid in data.GetModifiedElementIds():
            v = doc.GetElement(eid)
            if v is None or getattr(v, "IsTemplate", True):
                continue
            tid = v.ViewTemplateId
            t = None
            if tid != ElementId.InvalidElementId:
                t = doc.GetElement(tid)
            tname = t.Name if t is not None else "<None>"
            t_prot = t is not None and is_protected(doc, t)
            prev = vt.get(v.UniqueId)
            switched = prev is not None and (t is None or t.UniqueId != prev)
            pname = snap.get(prev, "a protected template") if prev else None

            # A view that did not exist at the last refresh is a NEW view (for
            # example a duplicate of a protected view). It starts out with the
            # template of the view it was copied from; that is allowed.
            known = _known.get(doc.Title)
            log("assign check: view '{}' template={} protected={} known-view={} "
                "had-protected={} unlocked={}".format(
                    v.Name, tname, t_prot,
                    known is None or v.UniqueId in known, prev is not None,
                    unlocked))
            if known is not None and v.UniqueId not in known:
                log("new view '{}' (template {}): allowed".format(v.Name, tname))
                known.add(v.UniqueId)
                if t_prot:
                    if clear_template_of_new_view(doc, v):
                        continue
                    vt_lock.audit(doc, "new-view", t.Name, u"view: " + v.Name)
                    vt[v.UniqueId] = t.UniqueId
                continue

            if prev is not None and not switched:
                # The view had this protected template before and still has it.
                # Revit touches the template setting of sibling / dependent views
                # when a view is duplicated; nothing was assigned or removed.
                continue

            if not unlocked and vt_lock.feature("template_assign"):
                if t_prot:
                    block(doc, eid, "Template '{}' is protected. "
                          "Ask a BIM admin to unlock it.".format(t.Name),
                          "blocked-assign", t.Name, u"view: " + v.Name)
                    return
                if switched:
                    block(doc, eid, "View '{}' uses protected template '{}'. "
                          "Ask a BIM admin to unlock it to change it."
                          .format(v.Name, pname),
                          "blocked-switch-away", pname,
                          u"view: {} -> {}".format(v.Name, tname))
                    return
            if switched:
                vt_lock.audit(doc, "switch-away", pname,
                              u"view: {} -> {}".format(v.Name, tname))
            elif t is not None and (t_prot or vt_lock.AUDIT_ALL_TEMPLATES):
                vt_lock.audit(doc, "assign", t.Name, u"view: " + v.Name)

        if unlocked:
            refresh(doc)       # keep the snapshot in step with allowed changes

    def GetUpdaterId(self):
        return self.uid

    def GetUpdaterName(self):
        return "VT Assign Guard"

    def GetAdditionalInformation(self):
        return "Blocks assigning or removing protected view templates"

    def GetChangePriority(self):
        return ChangePriority.Views


class VTDeleteGuard(IUpdater):
    """Blocks deleting a protected view template. Audits template deletions."""

    def __init__(self, addin_id):
        self.uid = UpdaterId(addin_id,
                             Guid("c5e1a8d2-7b34-4f90-b6a1-3e9d2f48c7a0"))

    def Execute(self, data):
        if _syncing["on"]:
            return
        doc = data.GetDocument()
        deleted = [e.IntegerValue for e in data.GetDeletedElementIds()]
        unlocked = vt_lock.is_unlocked(doc)
        get_snap(doc)
        known = _ids.get(doc.Title, {})
        allids = _allids.get(doc.Title, {})
        log("delete event: deleted={} known={} unlocked={}".format(
            deleted, sorted(known.keys()), unlocked))

        if not unlocked and vt_lock.feature("template_edit"):
            for eid in deleted:
                name = known.get(eid)
                if name is not None:
                    block(doc, any_live_element(doc),
                          "Template '{}' is protected and can't be deleted. "
                          "Ask a BIM admin to unlock it.".format(name),
                          "blocked-delete", name, "")
                    return

        for eid in deleted:
            prot_name = known.get(eid)
            name = prot_name or allids.get(eid)
            if name is None:
                continue
            if prot_name is not None or vt_lock.AUDIT_ALL_TEMPLATES:
                vt_lock.audit(doc, "delete", name, "")

        if unlocked:
            refresh(doc)       # prune the deleted template from the snapshot

    def GetUpdaterId(self):
        return self.uid

    def GetUpdaterName(self):
        return "VT Delete Guard"

    def GetAdditionalInformation(self):
        return "Blocks deleting protected view templates"

    def GetChangePriority(self):
        return ChangePriority.Views


# ---------------- graphic overrides in views with a protected template ----
_ov = {}        # doc title -> {view uniqueid: {"t": template uid, "o": {key: sig}}}
_ov_warned = set()


def protected_template_of(doc, v):
    """The protected template this view uses, or None."""
    try:
        if v is None or getattr(v, "IsTemplate", True):
            return None
        tid = v.ViewTemplateId
        if tid == ElementId.InvalidElementId:
            return None
        t = doc.GetElement(tid)
        if t is not None and is_protected(doc, t):
            return t
    except Exception:
        log_exc("protected_template_of")
    return None


def override_kinds():
    """The kinds of override that are switched on, as a tuple."""
    f = vt_lock.features()
    return tuple(k for k, name in (("e", "override_elements"),
                                   ("c", "override_categories"),
                                   ("f", "override_filters"),
                                   ("l", "override_links")) if f.get(name))


def _id_int(eid):
    try:
        return int(eid.IntegerValue)
    except Exception:
        return int(eid.Value)


def _link_settings(v, eid):
    try:
        return v.GetLinkOverrides(eid)
    except Exception:
        return None


def scan_overrides(doc, v):
    """{key: signature} for the graphic overrides set in this view:
    ('e', id) element, ('c', id) category / subcategory, ('f', id) filter.
    None when the view can't hold overrides or is too big to scan."""
    if not v.AreGraphicsOverridesAllowed():
        return None
    default = vt_lock.ogs_sig(OverrideGraphicSettings())
    out = {}
    kinds = override_kinds()
    if "e" in kinds:
        ids = list(FilteredElementCollector(doc, v.Id)
                   .WhereElementIsNotElementType().ToElementIds())
        if len(ids) > OVERRIDE_SCAN_LIMIT:
            if v.UniqueId not in _ov_warned:
                _ov_warned.add(v.UniqueId)
                log("override scan skipped for elements of '{}': {} elements "
                    "(limit {})".format(v.Name, len(ids), OVERRIDE_SCAN_LIMIT))
        else:
            for i in ids:
                s = vt_lock.ogs_sig(v.GetElementOverrides(i))
                if s != default:
                    out[("e", _id_int(i))] = s
    if "c" in kinds:
        for cat in doc.Settings.Categories:
            todo = [cat]
            try:
                todo.extend([c for c in cat.SubCategories])
            except Exception:
                pass
            for c in todo:
                try:
                    s = vt_lock.ogs_sig(v.GetCategoryOverrides(c.Id))
                except Exception:
                    continue
                if s != default:
                    out[("c", _id_int(c.Id))] = s
    if "f" in kinds:
        for fid in v.GetFilters():
            out[("f", _id_int(fid))] = vt_lock.ogs_sig(v.GetFilterOverrides(fid))
    if "l" in kinds:
        for inst in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            try:
                hidden = bool(inst.IsHidden(v))
            except Exception:
                hidden = None
            try:
                halftone = vt_lock.ogs_sig(v.GetElementOverrides(inst.Id))
            except Exception:
                halftone = None
            out[("l", _id_int(inst.Id))] = (
                vt_lock.link_sig(_link_settings(v, inst.Id)), hidden, halftone)
        for typ in FilteredElementCollector(doc).OfClass(RevitLinkType):
            out[("t", _id_int(typ.Id))] = (
                vt_lock.link_sig(_link_settings(v, typ.Id)),)
    return out


def baseline_view(doc, v):
    """Remember the overrides a view has now, if it uses a protected template
    and nothing is remembered for it yet. Done when a view is first opened."""
    t = protected_template_of(doc, v)
    if t is None:
        return
    store = _ov.setdefault(doc.Title, {})
    cur = store.get(v.UniqueId)
    if (cur is not None and cur["t"] == t.UniqueId
            and cur.get("k") == override_kinds()):
        return
    t0 = time.time()
    o = scan_overrides(doc, v)
    if o is None:
        return
    store[v.UniqueId] = {"t": t.UniqueId, "o": o, "k": override_kinds()}
    log("override baseline '{}': {} overrides, {:.0f} ms".format(
        v.Name, len(o), (time.time() - t0) * 1000))



class VTOverrideGuard(IUpdater):
    """Blocks new or changed graphic overrides in a view that uses a protected
    view template. Taking overrides away is always allowed."""

    def __init__(self, addin_id):
        self.uid = UpdaterId(addin_id,
                             Guid("e2b7a4c9-18d6-4a53-9f0b-6c3d85a1b7f2"))

    def Execute(self, data):
        if _syncing["on"]:
            return
        kinds = override_kinds()
        if not kinds:
            return
        doc = data.GetDocument()
        unlocked = vt_lock.is_unlocked(doc)
        store = _ov.setdefault(doc.Title, {})
        modified = [doc.GetElement(e) for e in data.GetModifiedElementIds()]
        template_edited = any(m is not None and getattr(m, "IsTemplate", False)
                              for m in modified)
        for eid, v in zip(list(data.GetModifiedElementIds()), modified):
            t = protected_template_of(doc, v)
            try:
                log("override guard: view changed '{}' template-view={} "
                    "protected-template={} unlocked={}".format(
                        v.Name, getattr(v, "IsTemplate", None),
                        t.Name if t is not None else None, unlocked))
            except Exception:
                pass
            if t is None:
                store.pop(v.UniqueId, None) if v is not None else None
                continue
            t0 = time.time()
            new = scan_overrides(doc, v)
            if new is None:
                continue
            ms = (time.time() - t0) * 1000
            old = store.get(v.UniqueId)
            if old is None or old["t"] != t.UniqueId or old.get("k") != kinds:
                # first time we see this view, its template just changed, or
                # the switched-on features changed
                log("override check '{}': baseline set ({} overrides, "
                    "{:.0f} ms)".format(v.Name, len(new), ms))
                store[v.UniqueId] = {"t": t.UniqueId, "o": new, "k": kinds}
                continue
            changed = vt_lock.override_changes(old["o"], new)
            log("override guard: '{}' changed keys={}".format(v.Name, changed))
            if ms > 1500:
                log("override check '{}' took {:.0f} ms".format(v.Name, ms))
            if not changed:
                store[v.UniqueId] = {"t": t.UniqueId, "o": new, "k": kinds}
                continue
            what = vt_lock.override_summary(changed)
            if not unlocked:
                block(doc, eid,
                      "View '{}' uses protected template '{}'. Graphic "
                      "overrides ({}) are not allowed in it. Ask a BIM "
                      "admin to unlock it.".format(v.Name, t.Name, what),
                      "blocked-override", t.Name,
                      u"view: {}; {}".format(v.Name, what))
                return
            if not template_edited:
                vt_lock.audit(doc, "override", t.Name,
                              u"view: {}; {}".format(v.Name, what))
            store[v.UniqueId] = {"t": t.UniqueId, "o": new, "k": kinds}

    def GetUpdaterId(self):
        return self.uid

    def GetUpdaterName(self):
        return "VT Override Guard"

    def GetAdditionalInformation(self):
        return "Blocks graphic overrides in views that use a protected template"

    def GetChangePriority(self):
        return ChangePriority.Views


log("classes defined")

up = None
up2 = None
up3 = None
up4 = None

try:
    up = VTGuard(HOST_APP.addin_id)
    if UpdaterRegistry.IsUpdaterRegistered(up.GetUpdaterId()):
        log("VT Guard already registered (old instance may be running)")
    else:
        UpdaterRegistry.RegisterUpdater(up, True)
        UpdaterRegistry.AddTrigger(up.GetUpdaterId(), ElementClassFilter(View),
                                   Element.GetChangeTypeAny())
        log("registered OK")
        try:
            UpdaterRegistry.AddTrigger(up.GetUpdaterId(),
                                       ElementClassFilter(View),
                                       Element.GetChangeTypeElementAddition())
            log("guard addition trigger OK")
        except Exception:
            log_exc("guard addition trigger")
except Exception:
    log_exc("register VT Guard")

try:
    up2 = VTAssignGuard(HOST_APP.addin_id)
    if UpdaterRegistry.IsUpdaterRegistered(up2.GetUpdaterId()):
        log("Assign guard already registered (old instance may be running)")
    else:
        UpdaterRegistry.RegisterUpdater(up2, True)
        UpdaterRegistry.AddTrigger(
            up2.GetUpdaterId(), ElementClassFilter(View),
            Element.GetChangeTypeParameter(
                ElementId(BuiltInParameter.VIEW_TEMPLATE)))
        log("assign guard registered OK")
except Exception:
    log_exc("register VT Assign Guard")

try:
    up3 = VTDeleteGuard(HOST_APP.addin_id)
    if UpdaterRegistry.IsUpdaterRegistered(up3.GetUpdaterId()):
        log("Delete guard already registered (old instance may be running)")
    else:
        UpdaterRegistry.RegisterUpdater(up3, True)
        UpdaterRegistry.AddTrigger(
            up3.GetUpdaterId(), ElementClassFilter(View),
            Element.GetChangeTypeElementDeletion())
        log("delete guard registered OK (View class trigger)")
        try:
            UpdaterRegistry.AddTrigger(
                up3.GetUpdaterId(),
                ElementCategoryFilter(BuiltInCategory.OST_Views),
                Element.GetChangeTypeElementDeletion())
            log("delete guard Views category trigger OK")
        except Exception:
            log_exc("delete guard category trigger")
except Exception:
    log_exc("register VT Delete Guard")


try:
    up4 = VTOverrideGuard(HOST_APP.addin_id)
    if UpdaterRegistry.IsUpdaterRegistered(up4.GetUpdaterId()):
        log("Override guard already registered (old instance may be running)")
    else:
        UpdaterRegistry.RegisterUpdater(up4, True)
        UpdaterRegistry.AddTrigger(up4.GetUpdaterId(),
                                   ElementClassFilter(View),
                                   Element.GetChangeTypeAny())
        log("override guard registered OK")
except Exception:
    log_exc("register VT Override Guard")


def on_failures(sender, args):
    try:
        if FAIL_ID is None:
            return
        acc = args.GetFailuresAccessor()
        for f in acc.GetFailureMessages():
            if f.GetFailureDefinitionId().Guid == FAIL_ID.Guid:
                args.SetProcessingResult(
                    FailureProcessingResult.ProceedWithRollBack)
                # after a Reload an older handler may also be attached: show
                # the message once (the flag lives in pyRevit's session store)
                from pyrevit import script
                now = time.time()
                if now - (script.get_envvar("VT_DIALOG_T") or 0) > 2:
                    script.set_envvar("VT_DIALOG_T", now)
                    TaskDialog.Show("Protected template", _pending["msg"] or
                                    "Protected template. Ask a BIM admin to unlock it.")
                return
    except Exception:
        log_exc("on_failures")


def on_doc_opened(sender, args):
    try:
        refresh(args.Document)
    except Exception:
        log_exc("on_doc_opened")
    vt_lock.flush_audit()


def on_sync_start(sender, args):
    try:
        sync_begin("synchronize with central")
    except Exception:
        log_exc("on_sync_start")


def on_sync_end(sender, args):
    try:
        sync_end(args.Document, "synchronize with central")
    except Exception:
        log_exc("on_sync_end")


def on_reload_start(sender, args):
    try:
        sync_begin("reload latest")
    except Exception:
        log_exc("on_reload_start")


def on_reload_end(sender, args):
    try:
        sync_end(args.Document, "reload latest")
    except Exception:
        log_exc("on_reload_end")


def on_doc_closing(sender, args):
    vt_lock.flush_audit()


def on_view_activated(sender, args):
    try:
        refresh(args.Document)
    except Exception:
        log_exc("on_view_activated")
    if override_kinds():
        try:
            baseline_view(args.Document, args.CurrentActiveView)
        except Exception:
            log_exc("override baseline")
    vt_lock.flush_audit()


_vg_binding = []


def make_vg_handler(command, feature_name):
    def handler(sender, args):
        """Visibility/Graphics (VV / VG) or an Override-by-... command is about
        to run."""
        try:
            if not vt_lock.feature(feature_name):
                return
            doc = HOST_APP.uiapp.ActiveUIDocument.Document
            v = doc.ActiveView
            t = protected_template_of(doc, v)
            if t is None or vt_lock.is_unlocked(doc):
                return
            vt_lock.audit(doc, "blocked-vg-dialog", t.Name, u"view: " + v.Name)
            log("blocked {} in view '{}'".format(command, v.Name))
            args.Cancel = True
            TaskDialog.Show("Protected template",
                            "Graphic overrides are locked in this view because "
                            "it uses protected template '{}'. Ask a BIM admin "
                            "to unlock it.".format(t.Name))
        except Exception:
            log_exc("vg handler " + command)
    return handler


try:
    from Autodesk.Revit.UI import RevitCommandId, PostableCommand
    for _n, _f in (("VisibilityOrGraphics", "block_vg_dialog"),
                   ("OverrideByCategory", "block_vg_dialog"),
                   ("OverrideByFilter", "block_vg_dialog"),
                   ("OverrideByElement", "override_elements")):
        try:
            _cid = RevitCommandId.LookupPostableCommandId(
                getattr(PostableCommand, _n))
            _b = HOST_APP.uiapp.CreateAddInCommandBinding(_cid)
            _b.BeforeExecuted += make_vg_handler(_n, _f)
            _vg_binding.append(_b)
            log("command lock hooked: " + _n)
        except Exception:
            log_exc("command lock " + _n)
except Exception:
    log_exc("command lock (import)")


def on_idle(sender, args):
    vt_lock.flush_audit()      # write queued audit entries (no-op when empty)
    if _syncing["on"] and time.time() - _syncing["since"] > 20:
        # Revit is idle, so no sync is running: the "finished" event was missed
        _syncing["on"] = False
        log("sync flag cleared by idle backstop")
    try:
        for u in (up, up2, up3, up4):
            if u is None:
                continue
            uid = u.GetUpdaterId()
            if not UpdaterRegistry.IsUpdaterEnabled(uid):
                UpdaterRegistry.EnableUpdater(uid)
                log("re-enabled " + u.GetUpdaterName())
    except Exception:
        pass
    try:
        uidoc = sender.ActiveUIDocument
        if uidoc is not None:
            doc = uidoc.Document
            if time.time() - _last.get(doc.Title, 0) > REFRESH_SEC:
                refresh(doc)
    except Exception:
        pass


try:
    HOST_APP.app.FailuresProcessing += on_failures
    HOST_APP.app.DocumentOpened += on_doc_opened
    HOST_APP.uiapp.Idling += on_idle
    log("events hooked")
except Exception:
    log_exc("event hooks")

try:
    HOST_APP.uiapp.ViewActivated += on_view_activated
    log("view activated hooked")
except Exception:
    log_exc("view activated hook")

for _ev, _fn in (("DocumentSynchronizingWithCentral", on_sync_start),
                 ("DocumentSynchronizedWithCentral", on_sync_end),
                 ("DocumentReloadingLatest", on_reload_start),
                 ("DocumentReloadedLatest", on_reload_end)):
    try:
        getattr(HOST_APP.app, _ev).__iadd__(_fn)
        log("hooked " + _ev)
    except Exception:
        log_exc("hook " + _ev)

try:
    HOST_APP.app.DocumentClosing += on_doc_closing
    log("document closing hooked")
except Exception:
    log_exc("document closing hook")

log("config folder: {} (found via {})".format(vt_lock.CONFIG_DIR, vt_lock.CONFIG_HOW))
log("audit: enabled={} dir={}".format(vt_lock.AUDIT_ENABLED, vt_lock.AUDIT_DIR))
try:
    log("rules file: {} exists={}".format(
        vt_lock.RULES_PATH, os.path.exists(vt_lock.RULES_PATH)))
    log("rules read: admins={} projects={}".format(
        len(vt_lock.load_rules().get("admins", [])),
        sorted(vt_lock.load_rules().get("projects", {}).keys())))
    st = vt_lock.rules_status()
    log("rules source={} file modified={}".format(st["source"], st["modified"]))
    if st["source"] != "server":
        log("WARNING: rules file could not be read, using the local cache "
            "(sync not finished, or the folder is not reachable)")
    if st["conflicts"]:
        log("WARNING: conflict copies in the config folder: {}".format(
            ", ".join(st["conflicts"])))
except Exception:
    log_exc("rules check")


# ---- Revit.ini: stop Revit auto-adding room/area/space tag leaders ----
try:
    import ini_fix
    ini_fix.ensure_room_tag_leader_disabled(log)
except Exception:
    log_exc("ini_fix")
