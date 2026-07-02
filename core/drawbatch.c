/*
 * core/drawbatch.c
 *
 * C extension that executes the per-frame mesh draw loop without returning
 * to the Python interpreter between calls.  On macOS the OpenGL→Metal
 * translation layer still applies, but the ~2-5 µs Python stack cost per
 * call is eliminated — for 60-100 draw calls per frame that is 0.1-0.5 ms
 * of Python overhead gone.
 *
 * No OpenGL headers are required at compile time.  The three function
 * pointers (glBindTexture, glBindVertexArray, glDrawArrays) are resolved
 * once at runtime by the Python side using ctypes and passed to
 * set_gl_functions().  This keeps the C file truly cross-platform with no
 * conditional includes.
 *
 * On Windows, OpenGL functions use the __stdcall calling convention; on
 * macOS and Linux they use the default (cdecl) convention.  GL_CALL handles
 * this portably.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

#ifdef _WIN32
  #define GL_CALL __stdcall
#else
  #define GL_CALL
#endif

typedef void (GL_CALL *pfn_BindTexture    )(unsigned int target, unsigned int texture);
typedef void (GL_CALL *pfn_BindVertexArray)(unsigned int array);
typedef void (GL_CALL *pfn_DrawArrays     )(unsigned int mode, int first, int count);

/* Cached once by set_gl_functions(), valid for the lifetime of the GL context. */
static pfn_BindTexture     _glBindTexture     = NULL;
static pfn_BindVertexArray _glBindVertexArray = NULL;
static pfn_DrawArrays      _glDrawArrays      = NULL;

#define GL_TEXTURE_2D 0x0DE1u
#define GL_TRIANGLES  0x0004u


/* set_gl_functions(bind_tex_ptr, bind_vao_ptr, draw_arrays_ptr)
 *
 * Cache the three GL function pointers.  Each argument is a plain Python
 * integer holding the address of the corresponding GL function.  Must be
 * called once after the GL context is current (so wglGetProcAddress / the
 * driver have already resolved them on the Python side). */
static PyObject *
set_gl_functions(PyObject *self, PyObject *args)
{
    unsigned long long bt, bva, da;
    if (!PyArg_ParseTuple(args, "KKK", &bt, &bva, &da))
        return NULL;
    _glBindTexture     = (pfn_BindTexture    )(uintptr_t)bt;
    _glBindVertexArray = (pfn_BindVertexArray)(uintptr_t)bva;
    _glDrawArrays      = (pfn_DrawArrays     )(uintptr_t)da;
    Py_RETURN_NONE;
}


/* draw_chunks(cmd_list)
 *
 * Solid pass: for each (tex_glo, vao_glo, vertex_count) tuple in cmd_list,
 * bind the texture, bind the VAO, and issue glDrawArrays — all without
 * returning to the Python interpreter between iterations. */
static PyObject *
draw_chunks(PyObject *self, PyObject *args)
{
    PyObject *cmd_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &cmd_list))
        return NULL;
    if (!_glBindTexture || !_glBindVertexArray || !_glDrawArrays) {
        PyErr_SetString(PyExc_RuntimeError,
            "drawbatch: call set_gl_functions() before draw_chunks()");
        return NULL;
    }

    Py_ssize_t n = PyList_GET_SIZE(cmd_list);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject    *item    = PyList_GET_ITEM(cmd_list, i);
        unsigned int tex_id  = (unsigned int)PyLong_AsUnsignedLong(PyTuple_GET_ITEM(item, 0));
        unsigned int vao_id  = (unsigned int)PyLong_AsUnsignedLong(PyTuple_GET_ITEM(item, 1));
        int          nverts  = (int)PyLong_AsLong              (PyTuple_GET_ITEM(item, 2));
        _glBindTexture    (GL_TEXTURE_2D, tex_id);
        _glBindVertexArray(vao_id);
        _glDrawArrays     (GL_TRIANGLES, 0, nverts);
    }
    Py_RETURN_NONE;
}


/* draw_chunks_wireframe(cmd_list)
 *
 * Wireframe pass: same list format as draw_chunks but skips texture
 * rebinding (glBindTexture), since wireframe ignores texture sampling. */
static PyObject *
draw_chunks_wireframe(PyObject *self, PyObject *args)
{
    PyObject *cmd_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &cmd_list))
        return NULL;
    if (!_glBindVertexArray || !_glDrawArrays) {
        PyErr_SetString(PyExc_RuntimeError,
            "drawbatch: call set_gl_functions() before draw_chunks_wireframe()");
        return NULL;
    }

    Py_ssize_t n = PyList_GET_SIZE(cmd_list);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject    *item   = PyList_GET_ITEM(cmd_list, i);
        unsigned int vao_id = (unsigned int)PyLong_AsUnsignedLong(PyTuple_GET_ITEM(item, 1));
        int          nverts = (int)PyLong_AsLong              (PyTuple_GET_ITEM(item, 2));
        _glBindVertexArray(vao_id);
        _glDrawArrays     (GL_TRIANGLES, 0, nverts);
    }
    Py_RETURN_NONE;
}


static PyMethodDef DrawBatchMethods[] = {
    {
        "set_gl_functions", set_gl_functions, METH_VARARGS,
        "set_gl_functions(bind_tex_ptr, bind_vao_ptr, draw_arrays_ptr)\n"
        "Cache GL function pointers as integers. Call once after context creation."
    },
    {
        "draw_chunks", draw_chunks, METH_VARARGS,
        "draw_chunks(cmd_list)\n"
        "Execute solid-pass draw calls for a list of (tex_glo, vao_glo, nverts) tuples."
    },
    {
        "draw_chunks_wireframe", draw_chunks_wireframe, METH_VARARGS,
        "draw_chunks_wireframe(cmd_list)\n"
        "Execute wireframe-pass draw calls (no texture bind) for the same list format."
    },
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef drawbatch_module = {
    PyModuleDef_HEAD_INIT,
    "drawbatch",
    "C draw-loop extension: executes OpenGL draw calls without Python per-call overhead.",
    -1,
    DrawBatchMethods
};

PyMODINIT_FUNC
PyInit_drawbatch(void)
{
    return PyModule_Create(&drawbatch_module);
}
