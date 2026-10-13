import { createContext, useContext } from 'solid-js';

export const ScopeContext = createContext('main');
export const useScope = () => useContext(ScopeContext);
