"""Measure a rendered MoM against the JSSD manual and print a pass/fail line per rule.

WHY THIS EXISTS. The layout was audited by eye, on page 1, and passed — while every even page of a
real 12-page document sat 0.8 in further left, because `w:mirrorMargins` swapped the binding margin on
the reverse side. An audit that reads one page proves nothing about the document. This measures EVERY
page of the rendered PDF and asserts the manual's own numbers.

HOW TO RUN (LibreOffice and pypdfium2 are both in the service image):

    docker run --rm -e PYTHONPATH=/app -w /app \
      -v "$PWD/app:/app:ro" -v "$PWD/tools:/tools:ro" -v /tmp/out:/out \
      --entrypoint python mom-prompt-service:local /tools/check_jssd_layout.py

It renders its own specimen document, so it needs no job, no model and no network.

WHAT IT CANNOT SEE. LibreOffice substitutes a serif face when Arial is absent, so glyph widths differ
slightly from Word; everything asserted here is a paragraph or page property (margins, indents,
alignment, line feeds, point size), which substitution does not move. Content quality — whether a
decision was really taken — is not a layout question and is not checked.

RULES, with the paragraph each comes from. Part 1 = JSSD Vol I Part 1, Ch 2; AD = Vol I Part 2, App AD.
"""
import subprocess
import sys

sys.path.insert(0, "/app")
from docx_export import build_mom_docx                                   # noqa: E402

PT = 72.0
LINE = 16.3 / PT            # one line feed at Arial 12 / 1.15 spacing, in inches (measured)
TOL = 0.05                  # inches; a line feed is 0.226 in, so this cannot confuse one gap for two

MARGIN = 1.30               # Part 1 paras 10.1-10.3: 0.5 in edge + 0.8 in gutter
TEXT_RIGHT = 7.77           # 8.27 - 0.5


def specimen():
    """A document that exercises every element the manual has a rule for."""
    points = [f"Point {i} was discussed, with the background and the present position set out at "
              f"length so that the paragraph wraps onto a second line." for i in range(26)]
    mom = {"title": "Quarterly Review", "date": "2026-04-21",
           "attendees": [{"name": "R K Menon", "role": "Chairman"},
                         {"name": "S Iyer", "role": "Member"},
                         {"name": "A B Sharma", "role": "Secretary"}],
           "agenda": ["Budget", "Training", "Maintenance"],
           "key_points": points,
           "key_figures": ["Rs 4.2 crore allotted.", "112 personnel trained."],
           "decisions": ["It was decided that the budget be revised.",
                         "It was decided to raise a second cadre."],
           "action_items": [{"task": "Submit the revised estimate", "assigned_to": "SO (Ops)",
                             "due": "30 Sep 26"}],
           "item_groups": [
               {"title": "Budget", "points": list(range(0, 9)), "decisions": [0], "figures": [0]},
               {"title": "Training", "points": list(range(9, 18)), "decisions": [1, 2], "figures": [1]},
               {"title": "Maintenance", "points": list(range(18, 26)), "decisions": [], "figures": []}]}
    meta = {"classification": "CONFIDENTIAL", "venue": "Commission Hall", "meeting_date": "2026-04-21",
            "meeting_time": "14:00", "amendments_by": "2026-09-30", "file_ref": "1234/5/Ops",
            "precedence": "PRIORITY", "copy_no": "3", "telephone": "20-2612345",
            "address": ["HQ Southern Command", "Pune", "411001"],
            "secretary": {"name": "A B Sharma", "rank": "Maj"},
            "distribution": [{"addressee": "HQ Western Command", "copies": "Two", "copy_no": "1-2",
                              "remarks": "By SDS"}]}
    return build_mom_docx(mom, meta)


def lines_of(pdf):
    """Every page as [(y_from_top, x_left, x_right, text)], sorted down the page."""
    out = []
    for page in pdf:
        h = page.get_height()
        tp = page.get_textpage()
        items = []
        for i in range(tp.count_rects()):
            l, b, r, t = tp.get_rect(i)
            txt = tp.get_text_bounded(left=l, bottom=b, right=r, top=t).strip()
            if txt:
                items.append(((h - t) / PT, l / PT, r / PT, txt))
        items.sort()
        out.append(items)
    return out


class Report:
    def __init__(self):
        self.failed = 0

    def check(self, ok, rule, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {rule}" + (f"   [{detail}]" if detail else ""))
        if not ok:
            self.failed += 1


def main():
    open("/out/_spec.docx", "wb").write(specimen())
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", "/out",
                    "/out/_spec.docx"], check=True, capture_output=True)
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument("/out/_spec.pdf")
    pages = lines_of(pdf)
    n = len(pages)
    r = Report()
    print(f"\nJSSD layout conformance — {n}-page specimen\n")

    # ── page and margins ────────────────────────────────────────────────────────────────────────
    w, h = pdf[0].get_width() / PT, pdf[0].get_height() / PT
    r.check(abs(w - 8.27) < 0.02 and abs(h - 11.69) < 0.02,
            "A4 paper (Part 1 para 6)", f"{w:.2f} x {h:.2f} in")

    lefts = []
    for i, items in enumerate(pages):
        body = [x0 for y, x0, x1, t in items if 0.8 < y < h - 0.8]
        lefts.append(round(min(body), 2))
    r.check(max(lefts) - min(lefts) < 0.03,
            "the binding margin stays on the LEFT of every page — no mirrored gutter "
            "(Part 1 paras 10.2-10.3; AD draws 0.8\"+0.5\" left on pp.328, 329 AND 330)",
            f"left edge per page: {lefts}")
    r.check(all(abs(x - MARGIN) < TOL for x in lefts),
            "left margin is 0.5 in + 0.8 in gutter = 1.30 in (Part 1 paras 10.1-10.3)",
            f"{lefts[0]:.2f} in")

    # ── classification, page count, page number ─────────────────────────────────────────────────
    tops = [items[0] for items in pages]
    bots = [items[-1] for items in pages]
    r.check(all(t[3] == "CONFIDENTIAL" for t in tops) and all(b[3] == "CONFIDENTIAL" for b in bots),
            "classification at the head AND foot of every page (Part 1 para 12.1)")
    centres = [round((t[1] + t[2]) / 2, 2) for t in tops]
    r.check(max(abs(c - (MARGIN + TEXT_RIGHT) / 2) for c in centres) < 0.1,
            "classification centred on the typed area (Part 1 para 12.1, Note 27)")

    p1 = pages[0]
    words = {1: "Only page", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven",
             8: "Eight", 9: "Nine"}
    want = "(Only page)" if n == 1 else f"({words.get(n, n)} pages)"      # numerals from ten up
    count = [it for it in p1 if it[3].startswith("(") and "page" in it[3].lower()]
    r.check(len(count) == 1 and count[0][3] == want,
            "page count on page 1, in words below ten, in brackets, not capitals "
            "(Part 1 paras 18.2, 18.4; Note 2 — CONFIDENTIAL and above)",
            f"{count[0][3] if count else 'missing'}, wanted {want}")
    if count:
        r.check(abs((count[0][0] - p1[0][0]) - LINE) < TOL,
                "ONE line feed between classification and page count (Part 1 para 18.1)",
                f"{(count[0][0] - p1[0][0]) / LINE:.2f} lines")

    nums = []
    for i, items in enumerate(pages):
        cand = [it for it in items[1:] if it[3].isdigit() and abs((it[1] + it[2]) / 2 - 4.54) < 0.15]
        nums.append(cand[0] if cand else None)
    r.check(nums[0] is None, "page 1 carries no page number (Part 1 para 16.2, Note 6)")
    r.check(all(nums[i] is not None and nums[i][3] == str(i + 1) for i in range(1, n)),
            "pages 2+ numbered in Arabic, centred (Part 1 paras 16, 16.3)",
            str([x[3] if x else None for x in nums]))
    if nums[1]:
        r.check(abs((nums[1][0] - pages[1][0][0]) - 2 * LINE) < TOL,
                "page number TWO line feeds below the classification (Part 1 para 16, Note 29)",
                f"{(nums[1][0] - pages[1][0][0]) / LINE:.2f} lines")

    # ── superscription (AD page 328 order and spacing) ──────────────────────────────────────────
    def find(page, prefix):
        return next((it for it in page if it[3].startswith(prefix)), None)

    tele, copy = find(p1, "Tele:"), find(p1, "Copy No")
    prec = next((it for it in p1 if it[3] == "PRIORITY"), None)
    addr1, fref = find(p1, "HQ Southern"), find(p1, "1234/5/Ops")
    r.check(tele and prec and abs(tele[0] - prec[0]) < 0.02,
            "telephone in line with the precedence (Ch 6 para 16.2)")
    r.check(prec and prec[2] > 7.0, "precedence at the right margin (Part 1 para 20.2.1)")
    r.check(copy and prec and abs((copy[0] - prec[0]) - 2 * LINE) < TOL,
            "copy number two line feeds below the precedence (AD; Ch 6 para 16.2)")
    r.check(abs((addr1[0] - copy[0]) - 2 * LINE) < TOL,
            "address two line feeds below the telephone block (Part 1 para 20.1.6, Note 10)")
    a2, a3 = find(p1, "Pune"), find(p1, "411001")
    r.check(abs((a2[0] - addr1[0]) - LINE) < TOL and abs((a3[0] - a2[0]) - LINE) < TOL,
            "address lines single-spaced (Part 1 para 8.2, Note 8)")
    r.check(abs((fref[0] - a3[0]) - 2 * LINE) < TOL,
            "file reference two line feeds below the address (Part 1 Note 12)")
    # "dt" alone, or "dt" and the date after it — the date, or "xxx...xxx" when none is given.
    dt = next((it for it in p1 if it[3] == "dt" or it[3].startswith("dt ")), None)
    r.check(dt and abs(dt[0] - fref[0]) < 0.02,
            "file reference and date on one line, joined by 'dt' (Part 1 paras 20.1.5, 20.1.7)")

    # ── title ───────────────────────────────────────────────────────────────────────────────────
    t1 = find(p1, "MINUTES OF THE MEETING")
    t2 = next((it for it in p1 if it[3].startswith("AT 1400 HR ON 21 APR 26")), None)
    r.check(t1 and abs((t1[0] - fref[0]) - 2 * LINE) < TOL,
            "title two line feeds below the superscription (Part 1 para 22)")
    r.check(t1 and abs((t1[1] + t1[2]) / 2 - (MARGIN + TEXT_RIGHT) / 2) < 0.1,
            "title centred on the typed area (Part 1 paras 22, 33.1.1)")
    r.check(t2 and abs((t2[0] - t1[0]) - LINE) < TOL,
            "title's second line single-spaced below the first (Part 1 para 22)")
    r.check(t2 is not None,
            "title carries the time as four figures + HR and the date as 'dd Mmm yy' "
            "(Part 1 paras 40, 42.2; AD note 2)", t2[3] if t2 else "missing")

    # ── paragraph and sub-paragraph geometry ────────────────────────────────────────────────────
    para1 = find(p1, "1.")
    lead = next((it for it in p1 if it[3].startswith("The following were present")), None)
    r.check(para1 and abs(para1[1] - MARGIN) < TOL,
            "paragraph number at the left margin (Part 1 para 33.3.2)")
    r.check(lead and abs(lead[1] - (MARGIN + 0.5)) < TOL,
            "0.5 in tab from the number to the text (Part 1 paras 33.3.6, 49.3)",
            f"{lead[1] - MARGIN:.2f} in")
    sub = find(p1, "1.1.")
    name = next((it for it in p1 if it[3] == "R K Menon"), None)
    r.check(sub and abs(sub[1] - (MARGIN + 0.5)) < TOL,
            "sub-paragraph number indented 0.5 in (Part 1 Note 30)")
    r.check(name and abs(name[1] - (MARGIN + 1.0)) < TOL,
            "sub-paragraph text a further 0.5 in (Part 1 para 33.4.4)")

    wrapped = None
    for items in pages:
        for j, it in enumerate(items):
            if it[3].startswith("Point 1 was discussed") and j + 1 < len(items):
                wrapped = items[j + 1]
    r.check(wrapped and abs(wrapped[1] - MARGIN) < TOL,
            "a paragraph's later lines return to the LEFT MARGIN, under the number, "
            "not hanging-indented (Part 1 paras 33.3.7, 49.4, Note 26)",
            f"{wrapped[1]:.2f} in" if wrapped else "not found")

    # ── attendees, items, closing ───────────────────────────────────────────────────────────────
    roles = [it[3] for it in pages[0] if it[3] in ("Chairman", "Secretary")]   # page 1 = the list
    r.check(roles == ["Chairman", "Secretary"],
            "chairman listed first, secretary last (Ch 6 para 16.4; AD note 3)", str(roles))

    heads = [it[3] for items in pages for it in items if it[3].startswith("ITEM ")]
    r.check(heads == ["ITEM I – BUDGET", "ITEM II – TRAINING", "ITEM III – MAINTENANCE"],
            "one ITEM per agenda entry, numbered in capital Roman (Ch 6 para 16.7)", str(heads))
    for items in pages:
        for j, it in enumerate(items):
            if it[3].startswith("ITEM ") and j + 1 < len(items):
                cls = items[j + 1]
                r.check(cls[3] == "(CONFIDENTIAL)" and abs((cls[0] - it[0]) - LINE) < TOL,
                        f"{it[3]}: its own classification in brackets, centred directly below "
                        f"(Ch 6 paras 16.1, 16.13)")
                break

    nums_seen = []
    for items in pages:
        for it in items:
            s = it[3].rstrip(".")
            if s.isdigit() and abs(it[1] - MARGIN) < TOL and it[0] > 1.0:
                nums_seen.append(int(s))
    body_nums = [x for x in nums_seen if x < 900]
    r.check(body_nums == list(range(1, len(body_nums) + 1)),
            "paragraph numbers run consecutively through the whole document, across ITEMs "
            "(Part 1 para 33.3.2)", f"1..{max(body_nums)}" if body_nums else "none")

    last = pages[-1]
    agree = next((it for it in last if it[3].startswith("Agreement with the minutes")), None)
    sig = next((it for it in last if it[3].startswith("(A B Sharma")), None)
    r.check(agree is not None, "amendments-by paragraph present (AD; Ch 6 para 15)")
    if agree and sig:
        cont = [it for it in last if it[0] > agree[0] and it[0] < sig[0]]
        gap = (sig[0] - (cont[-1][0] if cont else agree[0])) / LINE
        r.check(8.0 < gap < 10.0,
                "signature block 'as required, usually nine' line feeds below the text (AD)",
                f"{gap:.1f} lines")
    r.check(sig and abs(sig[1] - MARGIN) < TOL,
            "signature block left-aligned at the margin (AD; Part 1 para 27.1)")
    rank = next((it for it in last if it[3] == "Maj"), None)
    secy = next((it for it in last if it[3] == "Secretary" and it[0] > (sig[0] if sig else 0)), None)
    r.check(bool(sig and rank and secy),
            "signature block is three lines: (name), rank, appointment (Part 1 paras 27.1, Note 35)")

    dist = next((it for it in last if it[3] == "Distribution"), None)
    heads4 = [it[3] for it in last if it[3] in ("Distribution", "No of Copies", "Copy No", "Remarks")]
    r.check(len(heads4) == 4, "distribution table has AD's four columns, no 'Method' (AD p.330)",
            str(heads4))
    fil = next((it for it in last if it[3] == "File"), None)
    r.check(fil and dist and fil[0] > dist[0], "distribution list ends with File (AD p.330)")

    # ── type ────────────────────────────────────────────────────────────────────────────────────
    import pypdfium2.raw as pr
    sizes = set()
    for page in pdf:
        tp = page.get_textpage()
        for i in range(pr.FPDFText_CountChars(tp)):
            if tp.get_text_range(i, 1).strip():
                sizes.add(round(pr.FPDFText_GetFontSize(tp, i), 1))
    r.check(sizes == {12.0}, "one type size throughout (Part 1 para 13 names Arial, no size)",
            f"{sorted(sizes)} pt")

    print(f"\n{'ALL RULES PASS' if not r.failed else str(r.failed) + ' RULE(S) FAILED'}\n")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
