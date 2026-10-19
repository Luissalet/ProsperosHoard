import { createContext, useContext } from "react";

export const EditorCtx = createContext(null);
export const useEd = () => useContext(EditorCtx);
