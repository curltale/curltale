import { createContext, useContext } from 'react'
export const Studio = createContext(null)
export const useStudio = () => useContext(Studio)

export const RANK = { viewer: 0, modeller: 1, publisher: 2, admin: 3 }
// can(user, 'publisher'): the user's role is this one or above (viewer < modeller < publisher < admin)
export const can = (user, role) => role in RANK ? (RANK[user?.role] ?? 0) >= RANK[role] : (user?.roles || []).includes(role)   // editor and other extra roles are in user.roles
