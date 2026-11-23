import { fitNames, cleanNames, missingNames, editStartCount, MAX_NAME_BOXES } from "../components/dr/WorkerNamesInput";
import { dispositionFilename } from "./download";

describe("worker name helpers", () => {
  test("one box per worker, kept names first", () => {
    expect(fitNames(["A", "B"], 3)).toEqual(["A", "B", ""]);
    expect(fitNames(["A", "B", "C"], 2)).toEqual(["A", "B"]);
    expect(cleanNames(["  Ann   Lee ", ""], 2)).toEqual(["Ann Lee", ""]);
  });
  test("missing names counts blanks and workers over the box limit", () => {
    expect(missingNames(["A", " "], 3)).toBe(2);
    expect(missingNames(["A", "B"], 2)).toBe(0);
    expect(missingNames([], MAX_NAME_BOXES + 5)).toBe(MAX_NAME_BOXES + 5);
  });
  test("edit screen never starts below the saved names", () => {
    expect(editStartCount({ headcount: 0, worker_names: ["A", "B", "C"] })).toBe(3);
    expect(editStartCount({ headcount: 4, worker_names: ["A"] })).toBe(4);
    expect(editStartCount({})).toBe(1);
  });
});

describe("download file name", () => {
  test("prefers the accented UTF-8 name", () => {
    expect(dispositionFilename(`attachment; filename="Cafe.xlsx"; filename*=UTF-8''Caf%C3%A9.xlsx`)).toBe("Café.xlsx");
    expect(dispositionFilename('attachment; filename="Plain.xlsx"')).toBe("Plain.xlsx");
    expect(dispositionFilename("")).toBe(null);
  });
});
