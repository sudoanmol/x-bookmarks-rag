// Defuddle ships as ES modules across many files. Playwright needs one
// self-contained script, and an IIFE build has no global name of its own,
// so this shim gives it one.
import Defuddle from "defuddle/full";
globalThis.Defuddle = Defuddle;
