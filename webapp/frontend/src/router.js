import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'sessions', component: () => import('./views/SessionListView.vue') },
    { path: '/create', name: 'create', component: () => import('./views/SessionCreateView.vue') },
    { path: '/sessions/:id', name: 'detail', component: () => import('./views/SessionDetailView.vue'), props: true },
    { path: '/skills', name: 'skills', component: () => import('./views/SkillLibraryView.vue') },
  ],
})
