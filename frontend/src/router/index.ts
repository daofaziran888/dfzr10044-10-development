import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ReclaimVoucherView from '@/views/ReclaimVoucherView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/reclaim_voucher', name: 'reclaim_voucher', component: ReclaimVoucherView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
