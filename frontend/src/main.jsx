// the person's theme choice, applied before anything is drawn
try { const t = localStorage.getItem('ss_theme'); if (t === 'light' || t === 'dark') { document.documentElement.dataset.theme = t; document.querySelectorAll('link[rel=icon]').forEach((l) => { l.removeAttribute('media'); l.href = t === 'dark' ? '/favicon-light.png' : '/favicon-dark.png' }) } } catch {}
import React from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.jsx'
import '@fontsource-variable/bricolage-grotesque/opsz.css'     // the brand face of the wordmark, bundled: the app never calls a font host
import './styles.css'

createRoot(document.getElementById('root')).render(<BrowserRouter><App /></BrowserRouter>)
