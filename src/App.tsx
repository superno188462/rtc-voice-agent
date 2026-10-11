/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AuthProvider, useAuth } from './auth/AuthContext';
import RequireAuth from './auth/RequireAuth';
import AuthLoading from './auth/AuthLoading';
import MainPage from './pages/MainPage';
import RetrievalTestPage from './pages/RetrievalTest';
import LoginPage from './pages/Login';
import '@arco-design/web-react/dist/css/arco.css';

function AppRoutes() {
  const { user, loading } = useAuth();
  console.warn('运行问题可参考 README 内容进行排查');
  if (loading) return <AuthLoading />;
  return (
    <Routes>
      <Route path="/login" element={user ? <Navigate to="/" replace /> : <LoginPage />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <MainPage />
          </RequireAuth>
        }
      />
      <Route
        path="/retrieval-test"
        element={
          <RequireAuth>
            <RetrievalTestPage />
          </RequireAuth>
        }
      />
      <Route path="/knowledge-base" element={<Navigate to="/retrieval-test" replace />} />
      <Route path="*" element={<Navigate to={user ? '/' : '/login'} replace />} />
    </Routes>
  );
}

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}

export default App;
